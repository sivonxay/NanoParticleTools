from NanoParticleTools.machine_learning.core import SpectrumModelBase
from NanoParticleTools.machine_learning.modules.layer_interaction import InteractionBlock
from NanoParticleTools.machine_learning.modules.film import FiLMLayer
from NanoParticleTools.machine_learning.modules import NonLinearMLP
from torch_geometric.data.batch import Batch
from torch_geometric.data import HeteroData
from torch.nn import functional as F
from torch import nn
import torch
import torch_geometric.nn as gnn
import warnings
from typing import Dict, List
from sklearn.metrics.pairwise import euclidean_distances, cosine_similarity


class HeteroDCVRepresentationModule(torch.nn.Module):

    def __init__(self,
                 embed_dim: int = 16,
                 n_message_passing: int = 3,
                 nsigma: int = 5,
                 use_volume_in_dopant_constraint: bool = False,
                 normalize_interaction_by_volume: bool = False,
                 use_inverse_concentration: bool = False,
                 interaction_embedding: bool = False,
                 conc_eps: float = 0.01,
                 **kwargs):
        """
        Args:
            embed_dim: _description_.
            n_message_passing: _description_.
            nsigma: _description_.
            volume_normalization: _description_.
        """
        super().__init__()

        self.embed_dim = embed_dim
        self.n_message_passing = n_message_passing
        self.nsigma = nsigma
        self.use_volume_in_dopant_constraint = use_volume_in_dopant_constraint
        self.normalize_interaction_by_volume = normalize_interaction_by_volume
        self.use_inverse_concentration = use_inverse_concentration
        self.conc_eps = conc_eps
        self.interaction_embedding = interaction_embedding

        n_conc = 2 if self.use_inverse_concentration else 1
        interaction_dim = nsigma * 4 if self.use_inverse_concentration else nsigma

        self.dopant_embedder = nn.Embedding(3, embed_dim)
        self.dopant_film_layer = FiLMLayer(n_conc, embed_dim, [16, 16])

        if self.use_volume_in_dopant_constraint:
            self.dopant_constraint_film_layer = FiLMLayer(
                3, embed_dim, [16, 16])
        else:
            self.dopant_constraint_film_layer = FiLMLayer(
                2, embed_dim, [16, 16])
        self.dopant_norm = nn.BatchNorm1d(embed_dim)

        if self.interaction_embedding:
            self.interaction_embedder = nn.Embedding(9, embed_dim)
            self.intraaction_embedder = nn.Embedding(9, embed_dim)
        else:
            self.interaction_embedder = nn.Linear(2, embed_dim)
            self.intraaction_embedder = nn.Linear(2, embed_dim)
        self.integrated_interaction = InteractionBlock(nsigma=nsigma)
        self.interaction_norm = nn.BatchNorm1d(interaction_dim)
        self.intraaction_norm = nn.BatchNorm1d(interaction_dim)
        self.interaction_film_layer = FiLMLayer(interaction_dim, embed_dim,
                                                [16, 16])
        self.intraaction_film_layer = FiLMLayer(interaction_dim, embed_dim,
                                                [16, 16])

        self.convs = nn.ModuleList()
        for _ in range(n_message_passing):
            conv_modules = {
                ('dopant', 'coupled_to', 'interaction'):
                gnn.GATv2Conv(embed_dim,
                              embed_dim,
                              concat=False,
                              add_self_loops=False),
                ('interaction', 'coupled_to', 'dopant'):
                gnn.GATv2Conv(embed_dim,
                              embed_dim,
                              concat=False,
                              add_self_loops=False),
                ('dopant', 'coupled_to', 'intraaction'):
                gnn.GATv2Conv(embed_dim,
                              embed_dim,
                              concat=False,
                              add_self_loops=False),
                ('intraaction', 'coupled_to', 'dopant'):
                gnn.GATv2Conv(embed_dim,
                              embed_dim,
                              concat=False,
                              add_self_loops=False)
            }
            self.convs.append(gnn.HeteroConv(conv_modules))

        self.aggregation = gnn.aggr.SumAggregation()

    def forward(self,
                dopant_types,
                dopant_concs,
                dopant_constraint_indices,
                interaction_type_indices,
                interaction_types,
                interaction_dopant_indices,
                intraaction_type_indices,
                intraaction_types,
                intraaction_dopant_indices,
                edge_index_dict,
                radii,
                constraint_radii_idx,
                batch_dict=None):
        # Index the radii
        _radii = radii[constraint_radii_idx]

        # Compute the volumes of the constraints
        volume = 4 / 3 * torch.pi * _radii**3
        volume = volume[:, 1] - volume[:, 0]

        # Embed the dopant types
        dopant_attr = self.dopant_embedder(dopant_types)

        # Use a film layer to condition the dopant embedding on the dopant concentration
        conc_attr = dopant_concs.unsqueeze(-1)
        if self.use_inverse_concentration:
            inv_conc_attr = 1 / (conc_attr + self.conc_eps)
            conc_attr = torch.cat((conc_attr, inv_conc_attr), dim=-1)
        dopant_attr = self.dopant_film_layer(dopant_attr, conc_attr)

        # Condition the dopant node attribute on the size of the dopant constraint
        if self.use_volume_in_dopant_constraint:
            geometric_features = torch.cat(
                (_radii[dopant_constraint_indices],
                 volume[dopant_constraint_indices].unsqueeze(-1)),
                dim=-1)
        else:
            geometric_features = _radii[dopant_constraint_indices]
        dopant_attr = self.dopant_constraint_film_layer(
            dopant_attr, geometric_features)

        # Normalize the dopant node attribute
        dopant_attr = self.dopant_norm(dopant_attr)

        # Create an dictionary to allow for heterogenous message passing
        intermediate_x_dict = {
            'dopant': dopant_attr,
        }

        # Embed the interaction nodes, using the pair of dopant types
        if self.interaction_embedding:
            interaction_attr = self.interaction_embedder(interaction_types)
        else:
            interaction_attr = self.interaction_embedder(
                interaction_type_indices.float())

        # Index the radii and compute the integrated interaction
        interaction_node_radii = _radii[dopant_constraint_indices][
            interaction_dopant_indices].flatten(-2)
        integrated_interaction = self.integrated_interaction(
            *interaction_node_radii.T)

        # Multiply the concentration into the integrated_interaction
        interaction_node_conc = dopant_concs[interaction_dopant_indices]
        if self.use_inverse_concentration:
            inv_interaction_node_conc = 1 / (interaction_node_conc +
                                             self.conc_eps)

            # yapf: disable
            conc_factor = torch.stack(
                (interaction_node_conc[:, 0] * interaction_node_conc[:, 1],
                 interaction_node_conc[:, 0] * inv_interaction_node_conc[:, 1],
                 inv_interaction_node_conc[:, 0] * interaction_node_conc[:, 1],
                 inv_interaction_node_conc[:, 0] * inv_interaction_node_conc[:, 1])).mT
            # yapf: enable

            integrated_interaction = conc_factor[:, :,
                                                 None] * integrated_interaction[:,
                                                                                None, :]
            integrated_interaction = integrated_interaction.reshape(
                -1, 4 * self.nsigma)
        else:
            conc_factor = (interaction_node_conc[:, 0] *
                           interaction_node_conc[:, 1]).unsqueeze(-1)
            integrated_interaction = conc_factor * integrated_interaction

        # Normalize the integrated interaction
        # First by volume
        if self.normalize_interaction_by_volume:
            norm_factor = volume[dopant_constraint_indices][
                interaction_dopant_indices].prod(dim=1).unsqueeze(-1)
            integrated_interaction = integrated_interaction / norm_factor

        # Then using a batch norm
        integrated_interaction = self.interaction_norm(integrated_interaction)

        # Condition the interaction node attribute on the integrated interaction
        interaction_attr = self.interaction_film_layer(interaction_attr,
                                                       integrated_interaction)
        intermediate_x_dict['interaction'] = interaction_attr

        # Repeat the same process for the intraaction
        # Embed the interaction nodes, using the pair of dopant types
        if self.interaction_embedding:
            intraaction_attr = self.intraaction_embedder(
                intraaction_types)
        else:
            intraaction_attr = self.intraaction_embedder(
                intraaction_type_indices.float())

        # Index the radii and compute the integrated intraaction
        intraaction_node_radii = _radii[dopant_constraint_indices][
            intraaction_dopant_indices].flatten(-2)
        integrated_intraaction = self.integrated_interaction(
            *intraaction_node_radii.T)

        # Multiply the concentration into the integrated_intraaction
        intraaction_node_conc = dopant_concs[intraaction_dopant_indices]
        if self.use_inverse_concentration:
            inv_intraaction_node_conc = 1 / (intraaction_node_conc +
                                             self.conc_eps)

            # yapf: disable
            conc_factor = torch.stack(
                (intraaction_node_conc[:, 0] * intraaction_node_conc[:, 1],
                    intraaction_node_conc[:, 0] * inv_intraaction_node_conc[:, 1],
                    inv_intraaction_node_conc[:, 0] * intraaction_node_conc[:, 1],
                    inv_intraaction_node_conc[:, 0] * inv_intraaction_node_conc[:, 1])).mT
            # yapf: enable

            integrated_intraaction = conc_factor[:, :,
                                                 None] * integrated_intraaction[:,
                                                                                None, :]
            integrated_intraaction = integrated_intraaction.reshape(
                -1, 4 * self.nsigma)
        else:
            conc_factor = (intraaction_node_conc[:, 0] *
                           intraaction_node_conc[:, 1]).unsqueeze(-1)
            integrated_intraaction = conc_factor * integrated_intraaction

        # Normalize the integrated intraaction
        # First by volume
        if self.normalize_interaction_by_volume:
            norm_factor = volume[dopant_constraint_indices][
                intraaction_dopant_indices].prod(dim=1).unsqueeze(-1)
            integrated_intraaction = integrated_intraaction / norm_factor

        # Then using a batch norm
        integrated_intraaction = self.intraaction_norm(integrated_intraaction)

        # Condition the intraaction node attribute on the integrated intraaction
        intraaction_attr = self.intraaction_film_layer(intraaction_attr,
                                                       integrated_intraaction)
        intermediate_x_dict['intraaction'] = intraaction_attr

        # Apply the message passing operator(s)
        for conv in self.convs:
            intermediate_x_dict = conv(intermediate_x_dict, edge_index_dict)
            intermediate_x_dict = {
                k: nn.functional.silu(v)
                for k, v in intermediate_x_dict.items()
            }
        if batch_dict and 'dopant' in batch_dict:
            out = self.aggregation(intermediate_x_dict['dopant'],
                                   batch_dict['dopant'])
        else:
            out = self.aggregation(intermediate_x_dict['dopant'])

        return out


class HeteroDCVModel(SpectrumModelBase):

    def __init__(self,
                 embed_dim: int = 16,
                 n_message_passing: int = 3,
                 nsigma=5,
                 readout_layers: List[int] = [128],
                 **kwargs):
        if 'n_input_nodes' in kwargs:
            warnings.warn(
                'Cannot override n_input_nodes for this model. It is inferred from'
                'the embed_dim.')
            del kwargs['n_input_nodes']

        super().__init__(n_input_nodes=embed_dim, **kwargs)

        self.embed_dim = embed_dim
        self.n_message_passing = n_message_passing
        self.nsigma = nsigma
        self.readout_layers = readout_layers

        self.representation_module = HeteroDCVRepresentationModule(
            self.embed_dim, self.n_message_passing, self.nsigma, **kwargs)

        self.readout = NonLinearMLP(embed_dim, 1, self.readout_layers, 0.25, nn.SiLU)

    def forward(self,
                dopant_types,
                dopant_concs,
                dopant_constraint_indices,
                interaction_type_indices,
                interaction_types,
                interaction_dopant_indices,
                intraaction_type_indices,
                intraaction_types,
                intraaction_dopant_indices,
                edge_index_dict,
                radii,
                constraint_radii_idx,
                batch_dict=None):
        representation = self.representation_module(
            dopant_types, dopant_concs, dopant_constraint_indices,
            interaction_type_indices, interaction_types,
            interaction_dopant_indices, intraaction_type_indices,
            intraaction_types, intraaction_dopant_indices, edge_index_dict,
            radii, constraint_radii_idx, batch_dict)
        out = self.readout(representation)
        return out

    def get_inputs(self, data: HeteroData) -> Dict:

        input_dict = {'dopant_types': data['dopant'].types,
                      'dopant_concs': data['dopant'].x,
                      'dopant_constraint_indices': data['dopant'].constraint_indices,
                      'interaction_type_indices': data['interaction'].type_indices,
                      'interaction_types': data['interaction'].types,
                      'interaction_dopant_indices': data['interaction'].dopant_indices,
                      'intraaction_type_indices': data['intraaction'].type_indices,
                      'intraaction_types': data['intraaction'].types,
                      'intraaction_dopant_indices': data['intraaction'].dopant_indices,
                      'edge_index_dict': data.edge_index_dict,
                      'radii': data.radii,
                      'constraint_radii_idx': data.constraint_radii_idx,
                      'batch_dict': data.batch_dict}
        return input_dict

    def get_representation(self, data):
        reps = self.representation_module(**self.get_inputs(data))
        return reps

    def _evaluate_step(self, data):
        y_hat = self(**self.get_inputs(data))
        loss = self.loss_function(y_hat, data.log_y)
        return y_hat, loss

    def predict_step(self,
                     batch: HeteroData | Batch,
                     batch_idx: int | None = None) -> torch.Tensor:
        """
        Make a prediction for a batch of data.

        Args:
            batch (_type_): _description_
            batch_idx (int | None, optional): _description_. Defaults to None.

        Returns:
            torch.Tensor: _description_
        """
        y_hat = self(**self.get_inputs(batch))
        return y_hat

    def _step(self,
              prefix: str,
              batch: HeteroData | Batch,
              batch_idx: int | None = None,
              log: bool = True):
        y_hat, loss = self._evaluate_step(batch)

        # Determine the batch size
        if batch.batch_dict is not None:
            batch_size = batch.batch_dict["dopant"].size(0)
        else:
            batch_size = 1

        # Log the loss
        metric_dict = {f'{prefix}_loss': loss}
        if prefix != 'train':
            # For the validation and test sets, log additional metrics
            metric_dict[f'{prefix}_mse'] = F.mse_loss(y_hat, batch.log_y)
            metric_dict[f'{prefix}_mae'] = F.l1_loss(y_hat, batch.log_y)
            metric_dict[f'{prefix}_huber'] = F.huber_loss(y_hat, batch.log_y)
            metric_dict[f'{prefix}_hinge'] = F.hinge_embedding_loss(
                y_hat, batch.log_y)
            metric_dict[f'{prefix}_cos_sim'] = F.cosine_similarity(
                y_hat, batch.log_y, 1).mean(0)

        if log:
            self.log_dict(metric_dict, batch_size=batch_size)
        return loss, metric_dict

class AugmentHeteroDCVModel(HeteroDCVModel):

    def forward(self, input_dict: Dict, 
                augmented_input_dict: Dict
                ):
        dopant_types = input_dict['dopant_types']
        dopant_concs  = input_dict['dopant_concs']
        dopant_constraint_indices = input_dict['dopant_constraint_indices']
        interaction_type_indices = input_dict['interaction_type_indices']
        interaction_types = input_dict['interaction_types']
        interaction_dopant_indices = input_dict['interaction_dopant_indices']
        intraaction_type_indices = input_dict['intraaction_type_indices']
        intraaction_types = input_dict['intraaction_types']
        intraaction_dopant_indices = input_dict['intraaction_dopant_indices']
        edge_index_dict = input_dict['edge_index_dict']
        radii = input_dict['radii']
        constraint_radii_idx = input_dict['constraint_radii_idx']
        batch_dict = augmented_input_dict['batch_dict']


        subdivided_dopant_types = augmented_input_dict['dopant_types']
        subdivided_dopant_concs = augmented_input_dict['dopant_concs']
        subdivided_dopant_constraint_indices = augmented_input_dict['dopant_constraint_indices']
        subdivided_interaction_type_indices = augmented_input_dict['interaction_type_indices']
        subdivided_interaction_types = augmented_input_dict['interaction_types']
        subdivided_interaction_dopant_indices = augmented_input_dict['interaction_dopant_indices']
        subdivided_intraaction_type_indices = augmented_input_dict['intraaction_type_indices']
        subdivided_intraaction_types = augmented_input_dict['intraaction_types']
        subdivided_intraaction_dopant_indices = augmented_input_dict['intraaction_dopant_indices']
        subdivided_edge_index_dict = augmented_input_dict['edge_index_dict']
        subdivided_radii = augmented_input_dict['radii']
        subdivided_constraint_radii_idx = augmented_input_dict['constraint_radii_idx']
        subdivided_batch_dict = augmented_input_dict['batch_dict']

        representation = self.representation_module(
            dopant_types, dopant_concs, dopant_constraint_indices,
            interaction_type_indices, interaction_types,
            interaction_dopant_indices, intraaction_type_indices,
            intraaction_types, intraaction_dopant_indices, edge_index_dict,
            radii, constraint_radii_idx, batch_dict)
        out = self.readout(representation)

        subdivision_representation = self.representation_module(
            subdivided_dopant_types, subdivided_dopant_concs, subdivided_dopant_constraint_indices,
            subdivided_interaction_type_indices, subdivided_interaction_types,
            subdivided_interaction_dopant_indices, subdivided_intraaction_type_indices,
            subdivided_intraaction_types, subdivided_intraaction_dopant_indices, subdivided_edge_index_dict,
            subdivided_radii, subdivided_constraint_radii_idx, subdivided_batch_dict)
        subdivision_out = self.readout(subdivision_representation)

        return out, subdivision_out

    def get_inputs(self, data: HeteroData) -> Dict:

        input_dict = {'dopant_types': data['dopant'].types,
                      'dopant_concs': data['dopant'].x,
                      'dopant_constraint_indices': data['dopant'].constraint_indices,
                      'interaction_type_indices': data['interaction'].type_indices,
                      'interaction_types': data['interaction'].types,
                      'interaction_dopant_indices': data['interaction'].dopant_indices,
                      'intraaction_type_indices': data['intraaction'].type_indices,
                      'intraaction_types': data['intraaction'].types,
                      'intraaction_dopant_indices': data['intraaction'].dopant_indices,
                      'edge_index_dict': data.edge_index_dict,
                      'radii': data.radii,
                      'constraint_radii_idx': data.constraint_radii_idx,
                      'batch_dict': data.batch_dict}

        augmented_input_dict = {'dopant_types': data['subdivided_dopant'].types,
                                'dopant_concs': data['subdivided_dopant'].x,
                                'dopant_constraint_indices': data['subdivided_dopant'].constraint_indices,
                                'interaction_type_indices': data['subdivided_interaction'].type_indices,
                                'interaction_types': data['subdivided_interaction'].types,
                                'interaction_dopant_indices': data['subdivided_interaction'].dopant_indices,
                                'intraaction_type_indices': data['subdivided_intraaction'].type_indices,
                                'intraaction_types': data['subdivided_intraaction'].types,
                                'intraaction_dopant_indices': data['subdivided_intraaction'].dopant_indices,
                                'edge_index_dict': data.subdivided_edge_index_dict, #fix this
                                'radii': data.subdivided_radii,
                                'constraint_radii_idx': data.subdivided_constraint_radii_idx,
                                'batch_dict': data.subdivided_batch_dict}
        #iterate through subdivided_edge_index_dict and make sure keys match 
        for new_key, old_key in zip(data.edge_index_dict.keys(),data.subdivided_edge_index_dict.keys()):
            augmented_input_dict['edge_index_dict'][new_key] = augmented_input_dict['edge_index_dict'].pop(old_key)

        return input_dict, augmented_input_dict

    def get_representation(self, data: HeteroData):
        input_dict, augmented_input_dict = self.get_inputs(data)
        reps = self.representation_module(**input_dict)
        augmented_reps = self.representation_module(**augmented_input_dict)
        return reps, augmented_reps

    def _evaluate_step(self, data):
        input_dict, augmented_input_dict = self.get_inputs(data)
        rep, augmented_rep = self(input_dict, augmented_input_dict)
        rep = rep.detach().numpy()
        augmented_rep = augmented_rep.detach().numpy()
        loss = euclidean_distances(rep, augmented_rep)
        return rep, augmented_rep, loss

    def predict_step(self,
                     batch: HeteroData | Batch,
                     batch_idx: int | None = None) -> torch.Tensor:
        """
        Make a prediction for a batch of data.

        Args:
            batch (_type_): _description_
            batch_idx (int | None, optional): _description_. Defaults to None.

        Returns:
            torch.Tensor: _description_
        """
        y_hat, augmented_y_hat = self(self.get_inputs(batch))
        return y_hat

    def _step(self,
              prefix: str,
              batch: HeteroData | Batch,
              batch_idx: int | None = None,
              log: bool = True):
        rep, augmented_rep, loss = self._evaluate_step(batch)

        # Determine the batch size
        if batch.batch_dict is not None:
            batch_size = batch.batch_dict["dopant"].size(0)
        else:
            batch_size = 1

        # Log the loss
        metric_dict = {f'{prefix}_loss': loss}
        if prefix != 'train':
            # For the validation and test sets, log additional metrics
            metric_dict[f'{prefix}_cos_sim'] = cosine_similarity(
                rep, augmented_rep, 1).mean(0)
            #add other metrics if interested

        if log:
            self.log_dict(metric_dict, batch_size=batch_size)
        return loss, metric_dict
