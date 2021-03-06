import gc
import random
import resource
from copy import deepcopy

import numpy as np
from sklearn.base import BaseEstimator, TransformerMixin
from keras.callbacks import TensorBoard, ModelCheckpoint
from keras.utils.np_utils import to_categorical
from keras.preprocessing.sequence import pad_sequences

from utils import Tree
from models import (
    KerasModel,
    ParserModel,
)
from encoders import (
    FeaturesFactory,
    TargetsFactory,
)

from time import gmtime, strftime

class Parser(BaseEstimator, TransformerMixin, KerasModel):

    def __init__(self, params):
        self.params = params
        self.features_factory = FeaturesFactory(self.params)
        self.targets_factory = TargetsFactory(self.params)
        self.model = None

    def create(self):
        return ParserModel(
            self.params,
            self.features_factory,
            self.targets_factory,
        ).model

    def create_generator(self, batches, multiple):
        if not multiple:
            for batch in batches:
                yield batch
        else:
            n_batches = len(batches)
            batch_idx = 0
            while True:
                batch = batches[batch_idx]
                yield (batch[0], batch[1], [np.array(w) for w in batch[2]])
                batch_idx = (batch_idx + 1) % n_batches

    def batchify_X(self, trees):
        raw = self.features_factory.transform(trees)
        output = []

        words_batch = 0
        n_cols = len(raw)

        batch = [[] for _ in range(n_cols)]
        for row_idx, tree in enumerate(trees):
            words_batch += len(tree.tokens)

            for col_idx in range(n_cols):
                batch[col_idx].append(raw[col_idx][row_idx])

            if words_batch > self.params.batch_size:
                output_batch = [pad_sequences(x, padding='post', dtype='uint8') for x in batch]
                batch = [[] for _ in range(n_cols)]
                output.append(output_batch)
                words_batch = 0

        if words_batch > 0:
                output_batch = [pad_sequences(x, padding='post', dtype='uint8') for x in batch]
                output.append(output_batch)

        return output

    def batchify_y(self, trees):
        raw = self.targets_factory.transform(trees)
        output = []

        words_batch = 0
        n_cols = len(raw)

        batch = [[] for _ in range(n_cols)]
        for row_idx, tree in enumerate(trees):
            words_batch += len(tree.tokens)

            for col_idx in range(n_cols):
                batch[col_idx].append(raw[col_idx][row_idx])

            if words_batch > self.params.batch_size:
                padded_batch = [pad_sequences(x, padding='post', dtype='uint8') for x in batch]
                output_batch = []

                for target, padded_target in zip(self.params.targets, padded_batch):
                    if target == 'head':
                        output_batch.append(to_categorical(padded_target, num_classes=padded_target.shape[1], dtype='uint8'))
                    elif target in ['feats', 'sent']:
                        output_batch.append(padded_target)
                    else:
                        output_batch.append(to_categorical(padded_target, num_classes=self.targets_factory.encoders[target].vocab_size, dtype='uint8'))

                batch = [[] for _ in range(n_cols)]
                output.append(output_batch)
                words_batch = 0

        if words_batch > 0:
            padded_batch = [pad_sequences(x, padding='post') for x in batch]
            output_batch = []

            for target, padded_target in zip(self.params.targets, padded_batch):
                if target == 'head':
                    output_batch.append(to_categorical(padded_target, num_classes=padded_target.shape[1], dtype='uint8'))
                elif target in ['feats', 'sent']:
                    output_batch.append(padded_target)
                else:
                    output_batch.append(to_categorical(padded_target, num_classes=self.targets_factory.encoders[target].vocab_size, dtype='uint8'))

            batch = [[] for _ in range(n_cols)]
            output.append(output_batch)

        return output

    def batchify_weights(self, trees):
        output = []
        words_batch = 0
        n_cols = len(self.params.targets)
        batch = [[] for _ in range(n_cols)]
        for row_idx, tree in enumerate(trees):
            words_batch += len(tree.tokens)
            sample_weight = np.log(len(tree.tokens))
            if not self.params.train_partial or self.params.full_tree in tree.comments:
                targets = {
                    'head',
                    'deprel',
                    'lemma',
                    'upostag',
                    'xpostag',
                    'feats',
                    'semrel',
                }
            elif self.params.partial_tree in tree.comments:
                targets = {
                    'lemma',
                    'upostag',
                    'xpostag',
                    'feats',
                }
            else:
                targets = set()

            for col_idx, target in enumerate(self.params.targets):
                batch[col_idx].append(sample_weight if target in targets else 1e-9)

            if words_batch > self.params.batch_size:
                output.append(batch)
                batch = [[] for _ in range(n_cols)]
                words_batch = 0

        if words_batch > 0:
            output.append(batch)
            batch = [[] for _ in range(n_cols)]

        return output

    def fit(self, trees, shuffle=True):
        trees = sorted(trees, key=lambda x: len(x.tokens))

        if self.model is None:
            self.features_factory = self.features_factory.fit(trees)
            self.targets_factory = self.targets_factory.fit(trees)
            self.model = self.create()

        batches = list(zip(
                self.batchify_X(trees), 
                self.batchify_y(trees),
                self.batchify_weights(trees),
            ),
        )

        generator = self.create_generator(batches, multiple=True)
        run_name = strftime("%Y%m%dT%H%M%S", gmtime())

        callbacks = [
            TensorBoard('out/{}/'.format(run_name), update_freq='batch'),
            ModelCheckpoint('out/{}/weights'.format(run_name) + '.epoch{epoch:02d}-loss{loss:.2f}.hdf5', monitor='loss', verbose=1, save_best_only=True, mode='max')
        ]

        self.model.fit_generator(
            generator,
            callbacks=callbacks,
            steps_per_epoch=len(batches),
            epochs=self.params.epochs)

    def predict(self, trees):
        trees = sorted(trees, key=lambda x: len(x.tokens))
        output_trees = []

        tree_idx = 0
        for batch in self.batchify_X(trees):
            batch_trees = trees[tree_idx:(tree_idx + batch[0].shape[0])]
            batch_probs = self.model.predict_on_batch(batch)
            if not isinstance(batch_probs, list):
                batch_probs = [batch_probs]
            batch_preds = self.targets_factory.inverse_transform(batch_probs, batch_trees)

            for row_idx, old_tree in enumerate(batch_trees):
                row_probs = [p[row_idx] for p in batch_probs]
                row_preds = [p[row_idx] for p in batch_preds]

                emb = None
                new_tokens = []
                for token_idx, token in enumerate(old_tree.tokens):
                    new_token = deepcopy(token)
                    for field, pred in zip(self.params.targets, row_preds):
                        if field == 'sent':
                            emb = pred
                        else:
                            new_token.fields[field] = pred[token_idx]
                    new_tokens.append(new_token)

                output_trees.append(Tree(
                        tree_id=old_tree.id, 
                        tokens=new_tokens,
                        words=old_tree.words,
                        comments=old_tree.comments,
                        probs=row_probs if self.params.save_probs else None,
                        emb=emb,
                    ))
                tree_idx += 1

        output_trees = sorted(output_trees, key=lambda x: x.id)

        return output_trees
