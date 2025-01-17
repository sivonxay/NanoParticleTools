import torch
from torch import nn
from torch.nn import functional as F
from collections.abc import Callable
import pytorch_lightning as pl
from torch_geometric import nn as pyg_nn
from torch_geometric.data import Batch, Data, HeteroData
from torch_scatter.scatter import scatter
from NanoParticleTools.machine_learning.core import SpectrumModelBase
from NanoParticleTools.machine_learning.modules.core import LazyNonLinearMLP


class DiscreteGraphModel(SpectrumModelBase):

    def __init__(self,
                 input_channels=3,
                 n_output_nodes=400,
                 learning_rate: float = 1e-5,
                 lr_scheduler: Callable[[torch.optim.Optimizer],
                                        torch.optim.lr_scheduler._LRScheduler] | None = None,
                 loss_function: Callable[[list, list], float] = F.mse_loss,
                 dropout_probability: float = 0,
                 activation_module: torch.nn.Module = nn.SiLU,
                 mlp_layers: list[int] | None = None,
                 mpnn_module: torch.nn.Module = pyg_nn.GATv2Conv,
                 mpnn_kwargs: dict | None = None,
                 mpnn_operation: str = 'x, edge_index, edge_attr -> x',
                 mpnn_channels=[64, 128],
                 readout_operation: str = 'attn',
                 augment_loss: bool = False,
                 **kwargs):
        if mpnn_kwargs is None:
            mpnn_kwargs = {'edge_dim': 3}

        if mlp_layers is None:
            mlp_layers = [128, 256]
        super().__init__(**kwargs)

        self.n_output_nodes = n_output_nodes
        self.learning_rate = learning_rate
        self.dropout_probability = dropout_probability
        self.lr_scheduler = lr_scheduler
        self.loss_function = loss_function
        self.activation_module = activation_module
        self.augment_loss = augment_loss

        # Build the mpnn layers
        mpnn_modules = []
        for i, _ in enumerate(_mlp_channels := [input_channels] +
                              mpnn_channels):
            if i == len(_mlp_channels) - 1:
                break
            mpnn_modules.append((mpnn_module(*_mlp_channels[i:i + 2],
                                             **mpnn_kwargs), mpnn_operation))
            mpnn_modules.append(activation_module(inplace=True))

        # Build the mlp layers
        mlp_modules = []
        for i, _ in enumerate(mlp_sizes := [mpnn_channels[-1] * 2] +
                              mlp_layers + [n_output_nodes]):
            if i == len(mlp_sizes) - 1:
                break
            mlp_modules.append(nn.Dropout())
            mlp_modules.append(nn.Linear(*mlp_sizes[i:i + 2]))
            mlp_modules.append(activation_module(inplace=True))
        # Exclude the last activation, since this will inhibit learning
        mlp_modules = mlp_modules[:-1]

        # Initialize the weights to the linear layers according to Xavier Uniform
        for lin in mlp_modules:
            if isinstance(lin, nn.Linear):
                if activation_module == nn.SiLU:
                    torch.nn.init.xavier_uniform_(
                        lin.weight,
                        gain=1.519)  # 1.519 Seems like a good value for SiLU
                elif activation_module == nn.ReLU:
                    torch.nn.init.kaiming_uniform_(lin.weight,
                                                   nonlinearity='relu')

        if readout_operation.lower() == 'attn':
            # Use attention based Aggregation
            gate_nn = nn.Sequential(
                *[nn.Linear(mpnn_channels[-1], 1),
                  nn.Softmax(-2)])
            out_nn = nn.Linear(mpnn_channels[-1], 2 * mpnn_channels[-1])
            pyg_nn.AttentionalAggregation(gate_nn=gate_nn, nn=out_nn)

            readout = (pyg_nn.AttentionalAggregation(gate_nn=gate_nn,
                                                     nn=out_nn),
                       'x, batch -> x')
        elif readout_operation.lower() == 'set2set':
            # Use the Set2Set aggregation method to pool the
            # graph into a single global feature vector
            readout = (pyg_nn.aggr.Set2Set(mpnn_channels[-1],
                                           processing_steps=10),
                       'x, batch -> x')
        elif readout_operation.lower() == 'sum':
            # Use Sum Aggregation
            readout = (pyg_nn.aggr.SumAggregation(), 'x, batch -> x')
        else:
            # Default to using Mean Aggregation
            readout = (pyg_nn.aggr.MeanAggregation(), 'x, batch -> x')

        # Construct the primary module
        all_modules = mpnn_modules + [readout] + mlp_modules
        self.nn = pyg_nn.Sequential(
            'x, edge_index, edge_attr, edge_weight, batch', all_modules)

        self.save_hyperparameters()

    def forward(self, data):

        x = self.nn(x=data.x[:, :3],
                    edge_index=data.edge_index,
                    edge_attr=data.edge_attr,
                    edge_weight=data.edge_weight,
                    batch=data.batch)

        if data.batch is None:
            x = x.squeeze()

        return x
