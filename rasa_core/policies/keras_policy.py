from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import io
import json
import logging
import os
import warnings
import typing

from typing import Any, List, Dict, Text, Optional, Tuple

from rasa_core import utils
from rasa_core.policies import Policy
from rasa_core.featurizers import Featurizer

logger = logging.getLogger(__name__)

if typing.TYPE_CHECKING:
    import keras
    from rasa_core.domain import Domain
    from rasa_core.trackers import DialogueStateTracker


class KerasPolicy(Policy):
    SUPPORTS_ONLINE_TRAINING = True

    def __init__(self,
                 featurizer=None,  # type: Optional[Featurizer]
                 model=None,  # type: Optional[keras.models.Sequential]
                 graph=None,  # type: Optional[keras.backend.tf.Graph]
                 current_epoch=0  # type: int
                 ):
        # type: (...) -> None

        super(KerasPolicy, self).__init__(featurizer)

        if KerasPolicy.is_using_tensorflow() and not graph:
            from keras.backend import tf
            self.graph = tf.get_default_graph()
        else:
            self.graph = graph
        self.model = model
        self.current_epoch = current_epoch

    @property
    def max_len(self):
        if self.model:
            return self.model.layers[0].batch_input_shape[1]
        else:
            return None

    @staticmethod
    def is_using_tensorflow():
        from keras.backend import _BACKEND
        return _BACKEND == "tensorflow"

    def _build_model(self, num_features, num_actions, max_history_len):
        warnings.warn("Deprecated, use `model_architecture` instead.",
                      DeprecationWarning, stacklevel=2)
        return

    def model_architecture(
            self,
            input_shape,  # type: Tuple[int, int]
            output_shape  # type: Tuple[int, Optional[int]]
    ):
        # type: (...) -> keras.models.Sequential
        """Build a keras model and return a compiled model."""

        from keras.models import Sequential
        from keras.layers import \
            Masking, LSTM, Dense, TimeDistributed, Activation

        n_hidden = 32  # Neural Net and training params
        # Build Model
        model = Sequential()

        if len(output_shape) == 1:
            model.add(Masking(mask_value=-1, input_shape=input_shape))
            model.add(LSTM(n_hidden, dropout=0.2))
            model.add(Dense(input_dim=n_hidden, units=output_shape[-1]))
        elif len(output_shape) == 2:
            model.add(Masking(mask_value=-1,
                              input_shape=(None, input_shape[1])))
            model.add(LSTM(n_hidden, return_sequences=True, dropout=0.2))
            model.add(TimeDistributed(Dense(units=output_shape[-1])))
        else:
            raise ValueError("Cannot construct the model because"
                             "length of output_shape = {} "
                             "should be 1 or 2."
                             "".format(len(output_shape)))

        model.add(Activation('softmax'))

        model.compile(loss='categorical_crossentropy',
                      optimizer='rmsprop',
                      metrics=['accuracy'])

        logger.debug(model.summary())

        return model

    def train(self,
              training_trackers,  # type: List[DialogueStateTracker]
              domain,  # type: Domain
              **kwargs  # type: **Any
              ):
        # type: (...) -> Dict[Text: Any]

        max_training_samples = kwargs.get('max_training_samples')
        training_data = self.featurize_for_training(training_trackers,
                                                    domain,
                                                    max_training_samples)

        shuffled_X, shuffled_y = training_data.shuffled_X_y()

        self.model = self.model_architecture(shuffled_X.shape[1:],
                                             shuffled_y.shape[1:])

        validation_split = kwargs.get("validation_split", 0.0)
        logger.info("Fitting model with {} total samples and a validation "
                    "split of {}".format(training_data.num_examples(),
                                         validation_split))
        # filter out kwargs that cannot be passed to fit
        params = self._get_valid_params(self.model.fit, **kwargs)

        self.model.fit(shuffled_X, shuffled_y, **params)
        self.current_epoch = kwargs.get("epochs", 1)
        logger.info("Done fitting keras policy model")

        return training_data.metadata

    def continue_training(self, trackers, domain):
        # type: (List[DialogueStateTracker], Domain) -> None

        training_data = self.featurize_for_training(trackers,
                                                    domain)
        # fit to one extra example using updated trackers
        self.current_epoch += 1
        self.model.fit(training_data.X, training_data.y,
                       epochs=self.current_epoch + 1,
                       batch_size=len(trackers),
                       verbose=0,
                       initial_epoch=self.current_epoch)

    def predict_action_probabilities(self, tracker, domain):
        # type: (DialogueStateTracker, Domain) -> List[float]

        X, lengths = self.featurizer.create_X([tracker], domain)

        if KerasPolicy.is_using_tensorflow() and self.graph is not None:
            with self.graph.as_default():
                y_pred = self.model.predict(X, batch_size=1)
        else:
            y_pred = self.model.predict(X, batch_size=1)

        if len(y_pred.shape) == 2:
            return y_pred[-1].tolist()
        elif len(y_pred.shape) == 3:
            current_idx = lengths[0] - 1
            return y_pred[0, current_idx, :].tolist()

    def _persist_configuration(self, config_file):
        model_config = {
            "arch": "keras_arch.json",
            "weights": "keras_weights.h5",
            "epochs": self.current_epoch}

        utils.dump_obj_as_json_to_file(config_file, model_config)

    def persist(self, path):
        # type: (Text) -> None

        super(KerasPolicy, self).persist(path)

        if self.model:
            arch_file = os.path.join(path, 'keras_arch.json')
            weights_file = os.path.join(path, 'keras_weights.h5')
            config_file = os.path.join(path, 'keras_policy.json')

            # makes sure the model directory exists
            utils.create_dir_for_file(weights_file)
            utils.dump_obj_as_str_to_file(arch_file, self.model.to_json())

            self._persist_configuration(config_file)
            self.model.save_weights(weights_file, overwrite=True)
        else:
            warnings.warn("Persist called without a trained model present. "
                          "Nothing to persist then!")

    @classmethod
    def _load_model_arch(cls, path, meta):
        from keras.models import model_from_json

        arch_file = os.path.join(path, meta["arch"])
        if os.path.isfile(arch_file):
            with io.open(arch_file) as f:
                model = model_from_json(f.read())
            return model
        else:
            return None

    @classmethod
    def _load_weights_for_model(cls, path, model, meta):
        weights_file = os.path.join(path, meta["weights"])
        if model is not None and os.path.exists(weights_file):
            model.load_weights(weights_file)
        return model

    @classmethod
    def load(cls, path):
        # type: (Text) -> KerasPolicy
        if os.path.exists(path):
            featurizer = Featurizer.load(path)
            meta_path = os.path.join(path, "keras_policy.json")
            if os.path.isfile(meta_path):
                with io.open(meta_path) as f:
                    meta = json.loads(f.read())
                model_arch = cls._load_model_arch(path, meta)
                return cls(
                        featurizer=featurizer,
                        model=cls._load_weights_for_model(path,
                                                          model_arch,
                                                          meta),
                        current_epoch=meta["epochs"])
            else:
                return cls(featurizer=featurizer)
        else:
            raise Exception("Failed to load dialogue model. Path {} "
                            "doesn't exist".format(os.path.abspath(path)))
