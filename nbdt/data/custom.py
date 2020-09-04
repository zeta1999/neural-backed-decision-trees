import torchvision.datasets as datasets
import torch
import numpy as np
from torch.utils.data import Dataset
from collections import defaultdict
from nbdt.utils import DATASET_TO_NUM_CLASSES, DATASETS
from collections import defaultdict
from nbdt.graph import get_wnids, read_graph, get_leaves, get_non_leaves, \
    FakeSynset, get_leaf_to_path, wnid_to_synset, wnid_to_name
from nbdt.utils import (
    dataset_to_default_path_graph,
    dataset_to_default_path_wnids,
    hierarchy_to_path_graph)
from . import imagenet
import torch.nn as nn
import random


__all__ = names = ('CIFAR10IncludeLabels',
                   'CIFAR100IncludeLabels', 'TinyImagenet200IncludeLabels',
                   'Imagenet1000IncludeLabels', 'CIFAR10ExcludeLabels',
                   'CIFAR100ExcludeLabels', 'TinyImagenet200ExcludeLabels',
                   'Imagenet1000ExcludeLabels', 'CIFAR10ResampleLabels',
                   'CIFAR100ResampleLabels', 'TinyImagenet200ResampleLabels',
                   'Imagenet1000ResampleLabels')
keys = ('include_labels', 'exclude_labels', 'include_classes', 'probability_labels')


def add_arguments(parser):
    parser.add_argument('--probability-labels', nargs='*', type=float)
    parser.add_argument('--include-labels', nargs='*', type=int)
    parser.add_argument('--exclude-labels', nargs='*', type=int)
    parser.add_argument('--include-classes', nargs='*', type=int)


def dataset_to_dummy_classes(dataset):
    assert dataset in DATASETS
    num_classes = DATASET_TO_NUM_CLASSES[dataset]
    return [FakeSynset.create_from_offset(i).wnid for i in range(num_classes)]


class Node:

    def __init__(self, tree, wnid, other_class=False):
        self.tree = tree

        self.wnid = wnid
        self.synset = wnid_to_synset(wnid)

        self.parents = list(self.tree.G.pred[self.wnid])
        self.children = list(self.tree.G.succ[self.wnid])

        self.original_classes = tree.classes
        self.num_original_classes = len(self.tree.wnids_leaves)

        assert not self.is_leaf(), 'Cannot build dataset for leaf'
        self.has_other = other_class and not (self.is_root() or self.is_leaf())
        self.num_children = len(self.children)

        self.num_classes = self.num_children + int(self.has_other)

        self.old_to_new_classes, self.new_to_old_classes = \
            self.build_class_mappings()
        self.classes = self.build_classes()

        assert len(self.classes) == self.num_classes, (
            f'Number of classes {self.num_classes} does not equal number of '
            f'class names found ({len(self.classes)}): {self.classes}'
        )

        self.leaves = list(self.get_leaves())
        self.num_leaves = len(self.leaves)

    def wnid_to_class_index(self, wnid):
        return self.tree.wnids_leaves.index(wnid)

    @property
    def parent(self):
        if not self.parents:
            return None
        return self.parents[0]

    def get_leaves(self):
        return get_leaves(self.tree.G, self.wnid)

    def is_leaf(self):
        return len(self.children) == 0

    def is_root(self):
        return len(self.parents) == 0

    def build_class_mappings(self):
        old_to_new = defaultdict(lambda: [])
        new_to_old = defaultdict(lambda: [])
        for new_index, child in enumerate(self.children):
            for leaf in get_leaves(self.tree.G, child):
                old_index = self.wnid_to_class_index(leaf)
                old_to_new[old_index].append(new_index)
                new_to_old[new_index].append(old_index)
        if not self.has_other:
            return old_to_new, new_to_old

        new_index = self.num_children
        for old in range(self.num_original_classes):
            if old not in old_to_new:
                old_to_new[old].append(new_index)
                new_to_old[new_index].append(old)
        return old_to_new, new_to_old

    def build_classes(self):
        return [
            ','.join([self.original_classes[old] for old in old_indices])
            for new_index, old_indices in sorted(
                self.new_to_old_classes.items(), key=lambda t: t[0])
        ]

    @property
    def class_counts(self):
        """Number of old classes in each new class"""
        return [len(old_indices) for old_indices in self.new_to_old_classes]

    @staticmethod
    def dim(nodes):
        return sum([node.num_classes for node in nodes])


class Tree:

    def __init__(
            self, dataset, path_graph=None, path_wnids=None, classes=None,
            hierarchy=None):
        if dataset and hierarchy and not path_graph:
            path_graph = hierarchy_to_path_graph(dataset, hierarchy)
        if dataset and not path_graph:
            path_graph = dataset_to_default_path_graph(dataset)
        if dataset and not path_wnids:
            path_wnids = dataset_to_default_path_wnids(dataset)
        if dataset and not classes:
            classes = dataset_to_dummy_classes(dataset)

        self.dataset = dataset
        self.path_graph = path_graph
        self.path_wnids = path_wnids
        self.classes = classes
        self.G = read_graph(path_graph)
        self.wnids_leaves = get_wnids(path_wnids)
        self.wnid_to_class = {wnid: cls for wnid, cls in zip(self.wnids_leaves, self.classes)}

        self.wnid_to_node = self.get_wnid_to_node()
        self.wnids_nodes = sorted(self.wnid_to_node)
        self.inodes = [self.wnid_to_node[wnid] for wnid in self.wnids_nodes]

    @property
    def root(self):
        for node in self.inodes:
            if node.is_root():
                return node
        raise UserWarning('Should not be reachable. Tree should always have root')

    def get_wnid_to_node(self):
        wnid_to_node = {}
        for wnid in get_non_leaves(self.G):
            wnid_to_node[wnid] = Node(self, wnid)
        return wnid_to_node

    def get_leaf_to_path(self):
        node = self.inodes[0]
        leaf_to_path = get_leaf_to_path(self.G)
        wnid_to_node = {node.wnid: node for node in self.inodes}
        leaf_to_path_nodes = {}
        for leaf in leaf_to_path:
            leaf_to_path_nodes[leaf] = [
                {
                    'node': wnid_to_node.get(wnid, None),
                    'name': wnid_to_name(wnid)
                }
                for wnid in leaf_to_path[leaf]
            ]
        return leaf_to_path_nodes


class ResampleLabelsDataset(Dataset):
    """
    Dataset that includes only the labels provided, with a limited number of
    samples. Note that labels are integers in [0, k) for a k-class dataset.

    :drop_classes bool: Modifies the dataset so that it is only a m-way
                        classification where m of k classes are kept. Otherwise,
                        the problem is still k-way.
    """

    accepts_probability_labels = True

    def __init__(self, dataset, probability_labels=1, drop_classes=False, seed=0):
        self.dataset = dataset
        self.classes = dataset.classes
        self.labels = list(range(len(self.classes)))
        self.probability_labels = self.get_probability_labels(dataset, probability_labels)

        self.drop_classes = drop_classes
        if self.drop_classes:
            self.classes, self.labels = self.get_classes_after_drop(
                dataset, probability_labels)

        assert self.labels, 'No labels are included in `include_labels`'

        self.new_to_old = self.build_index_mapping(seed=seed)

    def get_probability_labels(self, dataset, ps):
        if not isinstance(ps, (tuple, list)):
            return [ps] * len(dataset.classes)
        if len(ps) == 1:
            return ps * len(dataset.classes)
        assert len(ps) == len(dataset.classes), (
            f'Length of probabilities vector {len(ps)} must equal that of the '
            f'dataset classes {len(dataset.classes)}.'
        )
        return ps

    def apply_drop(self, dataset, ps):
        classes = [
            cls for p, cls in zip(ps, dataset.classes)
            if p > 0
        ]
        labels = [i for p, i in zip(ps, range(len(dataset.classes))) if p > 0]
        return classes, labels

    def build_index_mapping(self, seed=0):
        """Iterates over all samples in dataset.

        Remaps all to-be-included samples to [0, n) where n is the number of
        samples with a class in the whitelist.

        Additionally, the outputted list is truncated to match the number of
        desired samples.
        """
        random.seed(seed)

        new_to_old = []
        for old, (_, label) in enumerate(self.dataset):
            if random.random() < self.probability_labels[label]:
                new_to_old.append(old)
        return new_to_old

    def __getitem__(self, index_new):
        index_old = self.new_to_old[index_new]
        sample, label_old = self.dataset[index_old]

        label_new = label_old
        if self.drop_classes:
            label_new = self.include_labels.index(label_old)

        return sample, label_new

    def __len__(self):
        return len(self.new_to_old)


class IncludeLabelsDataset(ResampleLabelsDataset):

    accepts_include_labels = True
    accepts_probability_labels = False

    def __init__(self, dataset, include_labels=(0,)):
        super().__init__(dataset, probability_labels=[
            int(cls in include_labels) for cls in range(len(dataset.classes))
        ])


class CIFAR10ResampleLabels(ResampleLabelsDataset):

    def __init__(self, *args, root='./data', probability_labels=1, **kwargs):
        super().__init__(
            dataset=datasets.CIFAR10(*args, root=root, **kwargs),
            probability_labels=probability_labels)


class CIFAR100ResampleLabels(ResampleLabelsDataset):

    def __init__(self, *args, root='./data', probability_labels=1, **kwargs):
        super().__init__(
            dataset=datasets.CIFAR100(*args, root=root, **kwargs),
            probability_labels=probability_labels)


class TinyImagenet200ResampleLabels(ResampleLabelsDataset):

    def __init__(self, *args, root='./data', probability_labels=1, **kwargs):
        super().__init__(
            dataset=imagenet.TinyImagenet200(*args, root=root, **kwargs),
            probability_labels=probability_labels)


class Imagenet1000ResampleLabels(ResampleLabelsDataset):

    def __init__(self, *args, root='./data', probability_labels=1, **kwargs):
        super().__init__(
            dataset=imagenet.Imagenet1000(*args, root=root, **kwargs),
            probability_labels=probability_labels)


class IncludeClassesDataset(IncludeLabelsDataset):
    """
    Dataset that includes only the labels provided, with a limited number of
    samples. Note that classes are strings, like 'cat' or 'dog'.
    """

    accepts_include_labels = False
    accepts_include_classes = True

    def __init__(self, dataset, include_classes=()):
        super().__init__(dataset, include_labels=[
                dataset.classes.index(cls) for cls in include_classes
            ])


class CIFAR10IncludeLabels(IncludeLabelsDataset):

    def __init__(self, *args, root='./data', include_labels=(0,), **kwargs):
        super().__init__(
            dataset=datasets.CIFAR10(*args, root=root, **kwargs),
            include_labels=include_labels)


class CIFAR100IncludeLabels(IncludeLabelsDataset):

    def __init__(self, *args, root='./data', include_labels=(0,), **kwargs):
        super().__init__(
            dataset=datasets.CIFAR100(*args, root=root, **kwargs),
            include_labels=include_labels)


class TinyImagenet200IncludeLabels(IncludeLabelsDataset):

    def __init__(self, *args, root='./data', include_labels=(0,), **kwargs):
        super().__init__(
            dataset=imagenet.TinyImagenet200(*args, root=root, **kwargs),
            include_labels=include_labels)


class Imagenet1000IncludeLabels(IncludeLabelsDataset):

    def __init__(self, *args, root='./data', include_labels=(0,), **kwargs):
        super().__init__(
            dataset=imagenet.Imagenet1000(*args, root=root, **kwargs),
            include_labels=include_labels)


class ExcludeLabelsDataset(IncludeLabelsDataset):

    accepts_include_labels = False
    accepts_exclude_labels = True

    def __init__(self, dataset, exclude_labels=(0,)):
        k = len(dataset.classes)
        include_labels = set(range(k)) - set(exclude_labels)
        super().__init__(
            dataset=dataset,
            include_labels=include_labels)


class CIFAR10ExcludeLabels(ExcludeLabelsDataset):

    def __init__(self, *args, root='./data', exclude_labels=(0,), **kwargs):
        super().__init__(
            dataset=datasets.CIFAR10(*args, root=root, **kwargs),
            exclude_labels=exclude_labels)


class CIFAR100ExcludeLabels(ExcludeLabelsDataset):

    def __init__(self, *args, root='./data', exclude_labels=(0,), **kwargs):
        super().__init__(
            dataset=datasets.CIFAR100(*args, root=root, **kwargs),
            exclude_labels=exclude_labels)


class TinyImagenet200ExcludeLabels(ExcludeLabelsDataset):

    def __init__(self, *args, root='./data', exclude_labels=(0,), **kwargs):
        super().__init__(
            dataset=imagenet.TinyImagenet200(*args, root=root, **kwargs),
            exclude_labels=exclude_labels)


class Imagenet1000ExcludeLabels(ExcludeLabelsDataset):

    def __init__(self, *args, root='./data', exclude_labels=(0,), **kwargs):
        super().__init__(
            dataset=imagenet.Imagenet1000(*args, root=root, **kwargs),
            exclude_labels=exclude_labels)
