import torch
from torch import nn
from torch.nn import functional as F
from typing import Callable, Union, Optional, List
import pytorch_lightning as pl
from torch_geometric import nn as pyg_nn
from torch_scatter.scatter import scatter

class CNNModel(pl.LightningModule):
    def __init__(self, 
                 n_output_nodes=400, 
                 learning_rate: Optional[float]=1e-5,
                 lr_scheduler: Optional[Callable[[torch.optim.Optimizer], torch.optim.lr_scheduler._LRScheduler]]=None,
                 loss_function: Optional[Callable[[List, List], float]] = F.mse_loss,
                 l2_regularization_weight: float = 0,
                 dropout_probability: float = 0, 
                 optimizer_type: str = 'SGD',
                 dimension: Optional[int] = 2,
                 activation_module: Optional[torch.nn.Module] = nn.ReLU, 
                 **kwargs):
        
        assert dimension > 0 and dimension <= 3
        if dimension == 1:
            conv_module = nn.LazyConv1d
            pool_module = nn.MaxPool1d
        elif dimension == 2:
            conv_module = nn.LazyConv1d
            pool_module = nn.MaxPool1d
        elif dimension == 3:
            conv_module = nn.LazyConv1d
            pool_module = nn.MaxPool1d

        super().__init__(**kwargs)
        
        self.img_dimension = dimension
        self.optimizer_type = optimizer_type
        self.learning_rate = learning_rate
        self.l2_regularization_weight = l2_regularization_weight
        self.dropout_probability = dropout_probability
        self.lr_scheduler = lr_scheduler
        self.loss_function = loss_function
        self.activation_module = activation_module
        
        modules = []
        modules.append(conv_module(out_channels=128, kernel_size=19, padding=9, stride=4))
        modules.append(activation_module())
        modules.append(pool_module(kernel_size=3, padding=1, stride=2))
        
        modules.append(conv_module(out_channels=32, kernel_size=11, padding=5, stride=4))
        modules.append(activation_module())
        modules.append(pool_module(kernel_size=3, padding=1, stride=2))
        
        modules.append(conv_module(out_channels=64, kernel_size=5, padding=2, stride=2))
        modules.append(activation_module())
        modules.append(pool_module(kernel_size=3, padding=1, stride=1))
        
        modules.append(conv_module(out_channels=64, kernel_size=3, padding=1, stride=1))
        modules.append(activation_module())
        modules.append(pool_module(kernel_size=3, padding=1, stride=2))
        
        modules.append(conv_module(out_channels=64, kernel_size=3, padding=1, stride=1))
        modules.append(activation_module())
        modules.append(pool_module(kernel_size=3, padding=1, stride=2))
        
        modules.append(nn.Flatten())
        modules.append(nn.Dropout(dropout_probability))
        modules.append(nn.LazyLinear(128))
        modules.append(activation_module())
        modules.append(nn.Dropout(dropout_probability))
        modules.append(nn.LazyLinear(n_output_nodes))
        modules.append(activation_module())
        self.nn = nn.Sequential(*modules)
        
        self.save_hyperparameters()

    def forward(self, data):
        x = data.x
        
        if len(data.x.shape) == self.img_dimension + 1:
            x = x.unsqueeze(dim=0)
             
        x = self.nn(x)
        
        if len(data.x.shape)  == self.img_dimension + 1:
            x = x.squeeze(dim=0)
        return x
    
    def configure_optimizers(self) -> Union[List[torch.optim.Optimizer], List[torch.optim.lr_scheduler._LRScheduler]]:
        """
        """ 
        # Default to the adam optimizer               
        if self.optimizer_type.lower() == 'sgd':
            optimizer = torch.optim.SGD(self.parameters(), lr=self.learning_rate, weight_decay=self.l2_regularization_weight)
        else:
            optimizer = torch.optim.Adam(self.parameters(), lr=self.learning_rate, weight_decay=self.l2_regularization_weight)

        if self.lr_scheduler is not None:
            lr_scheduler = self.lr_scheduler(optimizer)
        return [optimizer], [lr_scheduler]
    
    def layer_summary(self, x_shape):
        x = torch.randn(*x_shape)
        if len(x.shape) == self.img_dimension+1:
            x = x.unsqueeze(dim=0)
        
        for layer in self.nn:
            x = layer(x)
            print(layer.__class__.__name__, 'output shape:\t', x.shape)
    
    def _evaluate_step(self, 
                       data):
        y_hat = self(data)
        y = data.y
        loss = self.loss_function(y_hat, y)
        return y_hat, loss
    
    def training_step(self, 
                      batch, 
                      batch_idx):
        _, loss = self._evaluate_step(batch)
        self.log('train_loss', loss)
        return loss
    
    def validation_step(self, 
                        batch, 
                        batch_idx):
        _, loss = self._evaluate_step(batch)
        self.log('val_loss', loss)
        return loss
        
    def test_step(self, 
                  batch, 
                  batch_idx):
        _, loss = self._evaluate_step(batch)
        self.log('test_loss', loss)
        return loss
        
    def predict_step(self, 
                     batch, 
                     batch_idx):
        pred, _ = self._evaluate_step(batch)
        return pred

class DiscreteGraphModel(pl.LightningModule):
    def __init__(self,
                 input_channels=3,
                 n_output_nodes=400, 
                 learning_rate: Optional[float]=1e-5,
                 lr_scheduler: Optional[Callable[[torch.optim.Optimizer], torch.optim.lr_scheduler._LRScheduler]]=None,
                 loss_function: Optional[Callable[[List, List], float]] = F.mse_loss,
                 l2_regularization_weight: float = 0,
                 dropout_probability: float = 0, 
                 optimizer_type: str = 'SGD',
                 activation_module: Optional[torch.nn.Module] = nn.SiLU,
                 mlp_layers = [128, 256],
                 mpnn_channels = [64, 128],
                 mpnn_module: Optional[torch.nn.Module] = pyg_nn.GATv2Conv,
                 mpnn_kwargs: Optional[dict] = {'edge_dim':1},
                 mpnn_operation: Optional[str] = 'x, edge_index, edge_attr -> x',
                 aggregation_module: Optional[pyg_nn.Aggregation] = pyg_nn.aggr.Set2Set,
                 aggregation_kwargs: Optional[dict] = {'processing_step': 10},
                 **kwargs):
        super().__init__(**kwargs)
        
        self.optimizer_type = optimizer_type
        self.learning_rate = learning_rate
        self.l2_regularization_weight = l2_regularization_weight
        self.dropout_probability = dropout_probability
        self.lr_scheduler = lr_scheduler
        self.loss_function = loss_function
        self.activation_module = activation_module            
        
        # Build the mpnn layers
        mpnn_modules = []
        for i, _ in enumerate(_mlp_channels:=[input_channels]+mpnn_channels):
            if i == len(_mlp_channels)-1:
                 break
            mpnn_modules.append((mpnn_module(*_mlp_channels[i:i+2], **mpnn_kwargs), mpnn_operation))
            mpnn_modules.append(activation_module(inplace=True))
        
        # Build the mlp layers
        mlp_modules = []
        for i, _ in enumerate(mlp_sizes:=[mpnn_channels[-1]*2]+mlp_layers+[n_output_nodes]):
            if i == len(mlp_sizes)-1:
                 break
            mlp_modules.append(nn.Dropout())
            mlp_modules.append(nn.Linear(*mlp_sizes[i:i+2]))
            mlp_modules.append(activation_module(inplace=True))

        ## Initialize the weights to the linear layers according to Xavier Uniform
        for lin in mlp_modules:
            if activation_module == nn.SiLU:
                torch.nn.init.xavier_uniform_(lin.weight, gain=1.519) # 1.519 Seems like a good value for SiLU
            elif activation_module == nn.ReLU:
                torch.nn.init.xavier_uniform_(lin.weight, gain=torch.nn.init.calculate_gain('relu')) # Default to 1
            
        # Use the Set2Set aggregation method to pool the graph into a single global feature vector
        readout = aggregation_module(mpnn_channels[-1], **aggregation_kwargs)
        
        # Construct the primary module
        all_modules = mpnn_modules+[readout]+mlp_modules
        self.nn = pyg_nn.Sequential('x, edge_index, edge_attr, edge_weight',
                                    all_modules)
        
        self.save_hyperparameters()

    def forward(self, data):
        x = self.nn(x=data.x[:, :3], edge_index=data.edge_index, edge_attr=data.edge_attr, edge_weight=data.edge_weight)
        return x
    
    def configure_optimizers(self) -> Union[List[torch.optim.Optimizer], List[torch.optim.lr_scheduler._LRScheduler]]:
        """
        """ 
        # Default to the adam optimizer               
        if self.optimizer_type.lower() == 'sgd':
            optimizer = torch.optim.SGD(self.parameters(), lr=self.learning_rate, weight_decay=self.l2_regularization_weight)
        else:
            optimizer = torch.optim.Adam(self.parameters(), lr=self.learning_rate, weight_decay=self.l2_regularization_weight, amsgrad=True)

        if self.lr_scheduler is not None:
            lr_scheduler = self.lr_scheduler(optimizer)
        else:
            lr_scheduler = None
        return [optimizer], [lr_scheduler]
    
    def layer_summary(self, x_shape):
        x = torch.randn(*x_shape)
        if len(x.shape) == self.img_dimension+1:
            x = x.unsqueeze(dim=0)
        
        for layer in self.nn:
            x = layer(x)
            print(layer.__class__.__name__, 'output shape:\t', x.shape)
    
    def _evaluate_step(self, 
                       data):
        y_hat = self(data)
        if data.batch is not None:
            y = data.y.reshape(-1, 400)
        else:
            y = data.y
        loss = self.loss_function(y_hat, y)
        return y_hat, loss
    
    def training_step(self, 
                      batch, 
                      batch_idx):
        _, loss = self._evaluate_step(batch)
        self.log('train_loss', loss)
        return loss
    
    def validation_step(self, 
                        batch, 
                        batch_idx):
        _, loss = self._evaluate_step(batch)
        self.log('val_loss', loss)
        return loss
        
    def test_step(self, 
                  batch, 
                  batch_idx):
        _, loss = self._evaluate_step(batch)
        self.log('test_loss', loss)
        return loss
        
    def predict_step(self, 
                     batch, 
                     batch_idx):
        pred, _ = self._evaluate_step(batch)
        return pred