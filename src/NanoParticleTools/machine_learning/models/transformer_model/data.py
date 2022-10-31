from .._data import DataProcessor
import torch
from typing import List


class TransformerFeatureProcessor(DataProcessor):

    SPECIES_TYPE_INDEX = 0
    COMPOSITION_INDEX = 1
    VOLUME_INDEX = 2

    def __init__(self,
                 fields = ['formula_by_constraint', 'dopant_concentration', 'input.constraints'],
                 max_layers: int = 4,
                 possible_elements: List[str] = ['Yb', 'Er', 'Nd'],
                 volume_scale_factor = 1e-6,
                 **kwargs):
        """
        :param max_layers: 
        :param possible_elements:
        """
        super().__init__(fields=fields, **kwargs)
        
        self.max_layers = max_layers
        self.possible_elements = possible_elements
        self.volume_scale_factor = volume_scale_factor
        
    def process_doc(self, 
                    doc: dict) -> torch.Tensor:
        constraints = self.get_item_from_doc(doc, 'input.constraints')
        dopant_concentration = self.get_item_from_doc(doc, 'dopant_concentration')

        types = torch.tensor([j for i in range(self.max_layers) for j in range(len(self.possible_elements))])

        volumes = []
        compositions = []
        r_lower_bound = 0
        for layer in range(self.max_layers):
            try:
                if isinstance(constraints[layer], dict):
                    radius = constraints[layer]['radius']
                else:
                    radius = constraints[layer].radius
                volume = self.get_volume(radius) - self.get_volume(r_lower_bound)
                r_lower_bound = radius
                for i in range(len(self.possible_elements)):
                    volumes.append(volume * self.volume_scale_factor)
            except:
                for i in range(len(self.possible_elements)):
                    volumes.append(0)
            
            for el in self.possible_elements:
                try:
                    compositions.append(dopant_concentration[layer][el])
                except:
                    compositions.append(0)

        return {'x': torch.vstack([types, torch.tensor(volumes), torch.tensor(compositions)])}

    @property
    def is_graph(self):
        return False