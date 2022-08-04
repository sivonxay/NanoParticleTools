from NanoParticleTools.machine_learning.data import *
from pytorch_lightning.callbacks.early_stopping import EarlyStopping
from pytorch_lightning.callbacks import LearningRateMonitor
from pytorch_lightning.loggers import WandbLogger
import wandb
import pytorch_lightning as pl
import shutil
import os
import math
from ray import tune
from ray.tune.schedulers import ASHAScheduler
from maggma.stores.mongolike import MongoStore
from typing import Optional, Union

from NanoParticleTools.machine_learning.models import SpectrumAttentionModel, SpectrumModelBase
from NanoParticleTools.machine_learning.data import LabelProcessor, VolumeFeatureProcessor, NPMCDataModule
from NanoParticleTools.machine_learning.util.reporters import TrialTerminationReporter
from NanoParticleTools.inputs.nanoparticle import SphericalConstraint
from NanoParticleTools.util.visualization import plot_nanoparticle
from NanoParticleTools.machine_learning.util.learning_rate import get_sequential
from ray.tune.integration.pytorch_lightning import TuneReportCallback
import datetime
from matplotlib import pyplot as plt
import numpy as np
from fireworks.fw_config import LAUNCHPAD_LOC

def train_spectrum_model(config: dict, 
                         model_cls: SpectrumModelBase,
                         feature_processor: FeatureProcessor,
                         label_processor: Optional[LabelProcessor] = None,
                         num_epochs: Optional[int] = 10, 
                         num_gpus: Union[int, float] = 0,
                         wandb_project: Optional[str] = 'default_project',
                         wandb_save_dir: Optional[str] = os.environ['HOME'],
                         tune = False):
    """
    
    :param config: a config dictionary for the model
        ex - {'n_input_nodes': 20, 'n_output_nodes'; 200, 'n_hidden_layers': 2, 'n_hidden_1': 128, 'n_hidden_2': 128, 'learning_rate': 1e-3}
    :param num_epochs: max number of epochs to train the model. Early stopping may result in less training epochs
    :param num_gpus: number of gpus to use for the training. May be a fractional number if training multiple models concurrently on one gpu
    :param wandb_project: wandb_project name
    :param wandb_save_dir: Directory to save wandb files to
    :param tune: whether or not this is a tune experiment. (To make sure that TuneReportCallback is added)
    """
    if label_processor is None:
        label_processor = LabelProcessor(fields = ['output.spectrum_x', 'output.spectrum_y'], 
                                        spectrum_range = (-1000, 1000), 
                                        output_size = config['n_output_nodes'], 
                                        log = True,
                                        normalize = False)
    
    if os.path.exists('npmc_data.json'):
        data_module = NPMCDataModule(feature_processor=feature_processor, label_processor=label_processor, data_dir='npmc_data.json', batch_size=16)
    else:
        data_store = MongoStore.from_launchpad_file(LAUNCHPAD_LOC, 'avg_npmc_20220708')
        data_module = NPMCDataModule(feature_processor=feature_processor, label_processor=label_processor, data_store=data_store, batch_size=16)

    # Make the model
    model = model_cls(lr_scheduler=get_sequential,
                      optimizer_type='adam',
                      **config)
    
    # Make logger
    wandb_logger = WandbLogger(entity="esivonxay", 
                               project=wandb_project, 
                               save_dir=wandb_save_dir,
                               log_model=True)

    # Configure callbacks
    callbacks = []
    callbacks.append(LearningRateMonitor(logging_interval='step'))
    callbacks.append(EarlyStopping(monitor='val_loss', patience=200))
    if tune:
        callbacks.append(TuneReportCallback({"loss": "val_loss"}, on="validation_end"))
    

    # Run the training
    trainer = pl.Trainer(gpus = math.ceil(num_gpus), 
                         max_epochs=num_epochs, 
                         enable_progress_bar=False, 
                         logger=wandb_logger, 
                         callbacks=callbacks)
    try:
        trainer.fit(model=model, datamodule=data_module)
    except Exception as e:
        # Don't do anything with the exception to allow the wandb logger to finish
        print(e)
    
    columns = ['name', 'nanoparticle', 'spectrum', 'zoomed_spectrum', 'loss', 'npmc_qy', 'pred_qy']
    data = []
    rng = np.random.default_rng(seed = 10)
    for i in rng.choice(range(len(data_module.npmc_test)), 10, replace=False):
        (types, volumes, compositions), y = data_module.npmc_test[i]
        y_hat, loss = model._evaluate_step(data_module.npmc_test[i])
        
        npmc_spectrum = np.power(10, y.numpy())-1
        pred_spectrum = np.power(10, y_hat.detach().numpy())-1

        fig = plt.figure(dpi=150)
        plt.plot(data_module.label_processor.x, npmc_spectrum, label='NPMC', alpha=1)
        plt.plot(data_module.label_processor.x, pred_spectrum, label='NN', alpha=0.5)
        plt.xlabel('Wavelength (nm)', fontsize=18)
        plt.ylabel('Relative Intensity (a.u.)', fontsize=18)
        plt.xticks(fontsize=14)
        plt.yticks(fontsize=14)
        plt.legend()
        plt.tight_layout()
        
        fig.canvas.draw()
        full_fig_data = np.frombuffer(fig.canvas.tostring_rgb(), dtype=np.uint8)
        full_fig_data = full_fig_data.reshape(fig.canvas.get_width_height()[::-1] + (3,))
        
        
        plt.ylim(0, 1e4)
        fig.canvas.draw()
        fig_data = np.frombuffer(fig.canvas.tostring_rgb(), dtype=np.uint8)
        fig_data = fig_data.reshape(fig.canvas.get_width_height()[::-1] + (3,))
        
        constraints, dopant_specifications = get_np_template_from_feature(types, volumes, compositions, data_module.feature_processor)
        nanoparticle = plot_nanoparticle(constraints, dopant_specifications, as_np_array=True)
        
        npmc_qy = np.sum(npmc_spectrum[:int(npmc_spectrum.size/2)]) / np.sum(npmc_spectrum[int(npmc_spectrum.size/2):]) 
        pred_qy = np.sum(pred_spectrum[:int(pred_spectrum.size/2)]) / np.sum(pred_spectrum[int(pred_spectrum.size/2):])
        
        data.append([wandb_logger.experiment.name, 
                     wandb.Image(nanoparticle),
                     wandb.Image(full_fig_data), 
                     wandb.Image(fig_data), 
                     loss, 
                     npmc_qy,
                     pred_qy])
        # trainer.logger.experiment.log({'spectrum': fig})
    wandb_logger.log_table(key='sample_table', columns=columns, data=data)
    # Finalize the wandb logging
    wandb.finish()


def tune_npmc_asha(config: dict, 
                   model_cls: SpectrumModelBase,
                   feature_processor: FeatureProcessor,
                   label_processor: Optional[LabelProcessor] = None,
                   num_samples: Optional[int] = 10, 
                   num_epochs: Optional[int] = 1000, 
                   wandb_project: Optional[str] = None,
                   save_dir: Optional[str] = None,
                   resources_per_trial: Optional[dict] = {'cpu': 4}):
    """
    :param config: a config dictionary for the model
        ex - {'n_input_nodes': 20, 'n_output_nodes'; 200, 'n_hidden_layers': 2, 'n_hidden_1': 128, 'n_hidden_2': 128, 'learning_rate': 1e-3}
    :param num_epochs: max number of epochs to train the model. Early stopping may result in less training epochs
    :param num_gpus: number of gpus to use for the training. May be a fractional number if training multiple models concurrently on one gpu
    :param wandb_project: wandb_project name
    :param wandb_save_dir: Directory to save wandb files to
    """

    scheduler = ASHAScheduler(
        max_t=num_epochs,
        grace_period=min(max(num_epochs//10, 1), 100),
        reduction_factor=2)

    reporter = TrialTerminationReporter(
        parameter_columns=["nn_layers", "learning_rate", "l2_regularization_weight", "dropout_probability"],
        metric_columns=["loss", "training_iteration"])


    if wandb_project is None:
        date = ''.join([f'{val}{sym}'for val, sym in zip(datetime.datetime.now().isoformat().split('.')[0].split(':'), ['h', 'm', 's'])])
        wandb_project = f'Raytune-{date}'
    if save_dir is None:
        save_dir = os.path.join(os.environ['HOME'], 'train_output')

    train_fn_with_parameters = tune.with_parameters(train_spectrum_model,
                                                    model_cls = model_cls,
                                                    feature_processor = feature_processor,
                                                    label_processor = label_processor,
                                                    num_epochs = num_epochs,
                                                    num_gpus = resources_per_trial.get('gpu', 0),
                                                    wandb_project = wandb_project,
                                                    wandb_save_dir = save_dir,
                                                    tune = True)
    
    analysis = tune.run(train_fn_with_parameters,
                        resources_per_trial=resources_per_trial,
                        metric="loss",
                        mode="min",
                        config=config,
                        num_samples=num_samples,
                        scheduler=scheduler,
                        progress_reporter=reporter,
                        reuse_actors=False,
                        local_dir=save_dir,
                        name="tune_npmc_asha")

    print("Best hyperparameters found were: ", analysis.best_config)

def get_np_template_from_feature(types, volumes, compositions, feature_processor):
    possible_elements = feature_processor.possible_elements
    
    types = types.reshape(-1, len(possible_elements))
    compositions = compositions.reshape(-1, len(possible_elements))
    dopant_specifications = []
    for i in range(types.shape[0]):
        for j in range(types.shape[1]):
            dopant_specifications.append((i, compositions[i][j].item(), possible_elements[j], 'Y'))

    layer_volumes = volumes.reshape(-1, len(possible_elements))[:, 0]
    cum_volumes = torch.cumsum(layer_volumes, dim=0)
    radii = torch.pow(cum_volumes * 3 / (4 * np.pi), 1/3)*100
    constraints = [SphericalConstraint(radius.item()) for radius in radii]
    return constraints, dopant_specifications