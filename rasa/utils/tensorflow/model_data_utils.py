import typing
import copy
import numpy as np
import scipy.sparse
from collections import defaultdict, OrderedDict
from typing import List, Optional, Text, Dict, Tuple, Union, Any, DefaultDict

from rasa.nlu.constants import TOKENS_NAMES
from rasa.utils.tensorflow.model_data import Data, FeatureArray
from rasa.utils.tensorflow.constants import MASK, IDS, SENTENCE, SEQUENCE
from rasa.shared.nlu.training_data.message import Message
from rasa.shared.nlu.constants import (
    TEXT,
    ENTITIES,
    ENTITY_ATTRIBUTE_TYPE,
    ENTITY_ATTRIBUTE_GROUP,
    ENTITY_ATTRIBUTE_ROLE,
)

if typing.TYPE_CHECKING:
    from rasa.shared.nlu.training_data.features import Features
    from rasa.nlu.extractors.extractor import EntityTagSpec

TAG_ID_ORIGIN = "tag_id_origin"


def extract_attribute_features_from_message(
    message: Message, attribute: Text, featurizers: Optional[List[Text]] = None,
) -> Dict[Tuple[bool, Text], Union[scipy.sparse.spmatrix, np.ndarray]]:
    """Extracts and combines features from the given messages.

    Args:
        message: any message
        attribute: attribute for which features should be collected
        featurizers: the list of featurizers to consider

    Returns:
        a dictionary mapping keys that take the form
        `([True|False],[SEQUENCE|SENTENCE])' to a sparse or dense matrix
        where `True` or `False` indicates that the matrix is sparse or dense,
        respectively

    Raises:
        `ValueError`s in case the extracted sentence features or the extracted
        sequence features do not align in terms of their last dimension, respectively
    """
    (sparse_sequence_features, sparse_sentence_features,) = message.get_sparse_features(
        attribute, featurizers
    )
    dense_sequence_features, dense_sentence_features = message.get_dense_features(
        attribute, featurizers
    )

    if (
        dense_sequence_features is not None
        and sparse_sequence_features is not None
        and (
            dense_sequence_features.features.shape[0]
            != sparse_sequence_features.features.shape[0]
        )
    ):
        raise ValueError(
            f"Sequence dimensions for sparse and dense sequence features "
            f"don't coincide in '{message.get(TEXT)}'"
            f"for attribute '{attribute}'."
        )
    if (
        dense_sentence_features is not None
        and sparse_sentence_features is not None
        and (
            dense_sentence_features.features.shape[0]
            != sparse_sentence_features.features.shape[0]
        )
    ):
        raise ValueError(
            f"Sequence dimensions for sparse and dense sentence features "
            f"don't coincide in '{message.get(TEXT)}'"
            f"for attribute '{attribute}'."
        )

    out = {}
    if sparse_sentence_features is not None:
        out[(True, SENTENCE)] = sparse_sentence_features.features
    if sparse_sequence_features is not None:
        out[(True, SEQUENCE)] = sparse_sequence_features.features
    if dense_sentence_features is not None:
        out[(False, SENTENCE)] = dense_sentence_features.features
    if dense_sequence_features is not None:
        out[(False, SEQUENCE)] = dense_sequence_features.features

    return out


def extract_attribute_features_from_all_messages(
    messages: List[Message],
    attribute: Text,
    type: Optional[Text] = None,
    featurizers: Optional[List[Text]] = None,
) -> Tuple[List[FeatureArray], List[FeatureArray]]:
    """Collects and combines the attribute related features from all messages.

    Args:
        messages: list of messages to extract features from
        attribute: the only attribute which will be considered
        type: If set to `'sequence'` or `'sentence'`, then only the chosen type of
          feature will be considered. If set to `None`, then sequence and sentence
          type features will be considered.
        featurizers: the featurizers to be considered

    Returns:
        Sequence level features and sentence level features. Each feature contains
        FeatureArrays with sparse features first.

    Raises:
       a `ValueError` in case `type` is not either `'sequence'`, `'sentence'` or None,
       or in case the types of the features extracted from the given messages have a
       type that is neither `'sequence'` nor `'sentence'`
    """
    if (type is not None) and (type not in [SENTENCE, SEQUENCE]):
        raise ValueError(
            f"Expected type to be None, {SENTENCE} or {SEQUENCE} but found {type}."
        )
    # for each label_example, collect sparse and dense feature (matrices) in lists
    collected_features: Dict[
        Tuple[bool, Text], List[Union[np.ndarray, scipy.sparse.spmatrix]]
    ] = dict()
    for msg in messages:
        is_sparse_and_type_to_feature = extract_attribute_features_from_message(
            message=msg, attribute=attribute, featurizers=featurizers,
        )
        for (
            (feat_is_sparse, feat_type),
            feat_mat,
        ) in is_sparse_and_type_to_feature.items():
            if feat_type not in [SEQUENCE, SENTENCE]:
                raise ValueError(
                    f"Expected types of extracted features to be {SENTENCE} or "
                    f"{SEQUENCE} but found {feat_type}."
                )
            if (type is None) or (type == feat_type):
                collected_features.setdefault((feat_is_sparse, feat_type), []).append(
                    feat_mat
                )

    # finally wrap the lists of feature_matrices into FeatureArrays
    # and collect the resulting arrays in one list per type:
    sequence_features = []
    sentence_features = []
    for type, collection in [
        (SEQUENCE, sequence_features),
        (SENTENCE, sentence_features),
    ]:
        for is_sparse in [True, False]:
            # Note: the for loops make the order explicit, which would
            # otherwise (i.e. iteration over collected_features) depend on
            # insertion order inside _extract_features
            list_of_matrices = collected_features.get((is_sparse, type), None)
            if list_of_matrices:
                collection.append(
                    FeatureArray(np.array(list_of_matrices), number_of_dimensions=3)
                )
    return sequence_features, sentence_features


def featurize_training_examples(
    training_examples: List[Message],
    attributes: List[Text],
    entity_tag_specs: Optional[List["EntityTagSpec"]] = None,
    featurizers: Optional[List[Text]] = None,
    bilou_tagging: bool = False,
    type: Optional[Text] = None,
) -> Tuple[List[Dict[Text, List["Features"]]], Dict[Text, Dict[Text, List[int]]]]:
    """Converts training data into a list of attribute to features.

    Possible attributes are, for example, `INTENT`, `RESPONSE`, `TEXT`, `ACTION_TEXT`,
    `ACTION_NAME` or `ENTITIES`.
    Also returns sparse feature sizes for each attribute. It could look like this:
    `{TEXT: {FEATURE_TYPE_SEQUENCE: [16, 32], FEATURE_TYPE_SENTENCE: [16, 32]}}`.

    Args:
        training_examples: the list of training examples
        attributes: the attributes to consider
        entity_tag_specs: the entity specs
        featurizers: the featurizers to consider
        bilou_tagging: indicates whether BILOU tagging should be used or not
        type: If set to `'sequence'` or `'sentence'`, then only the chosen type of
          feature will be considered. If set to `None`, then sequence and sentence
          type features will be considered.

    Returns:
        A list of attribute to features.
        A dictionary of attribute to feature sizes.

    Raises:
       a `ValueError` in case `type` is not either `'sequence'`, `'sentence'` or None
    """
    if (type is not None) and (type not in [SEQUENCE, SENTENCE]):
        raise ValueError(
            f"Expected type to be None, {SENTENCE} or {SEQUENCE} but found {type}."
        )
    output = []

    for example in training_examples:
        attribute_to_features = {}
        for attribute in attributes:
            if attribute == ENTITIES:
                attribute_to_features[attribute] = []
                # in case of entities add the tag_ids
                for tag_spec in entity_tag_specs:
                    attribute_to_features[attribute].append(
                        get_tag_ids(example, tag_spec, bilou_tagging)
                    )

            elif attribute in example.data:
                attribute_to_features[attribute] = example.get_all_features(
                    attribute, featurizers
                )
            if type:  # filter results by type
                attribute_to_features[attribute] = [
                    f for f in attribute_to_features[attribute] if f.type == type
                ]
        output.append(attribute_to_features)
    sparse_feature_sizes = {}
    if output and training_examples:
        sparse_feature_sizes = _collect_sparse_feature_sizes(
            featurized_example=output[0],
            training_example=training_examples[0],
            featurizers=featurizers,
            type=type,
        )
    return output, sparse_feature_sizes


def _collect_sparse_feature_sizes(
    featurized_example: Dict[Text, List["Features"]],
    training_example: Message,
    featurizers: Optional[List[Text]] = None,
    type: Optional[Text] = None,
) -> Dict[Text, Dict[Text, List[int]]]:
    """Collects sparse feature sizes for all attributes that have sparse features.

    Returns sparse feature sizes for each attribute. It could look like this:
    `{TEXT: {FEATURE_TYPE_SEQUENCE: [16, 32], FEATURE_TYPE_SENTENCE: [16, 32]}}`.

    Args:
        featurized_example: a featurized example
        training_example: a training example
        featurizers: the featurizers to consider
        type: the feature type to consider; if set to None, all types will be
          considered

    Returns:
        A dictionary of attribute to feature sizes.

    Raises:
       a `ValueError` in case `type` is not either `'sequence'`, `'sentence'` or None
    """
    if (type is not None) and (type not in [SEQUENCE, SENTENCE]):
        raise ValueError(
            f"Expected type to be None, {SENTENCE} or {SEQUENCE} but found {type}."
        )
    sparse_feature_sizes = {}
    sparse_attributes = []
    for attribute, features in featurized_example.items():
        if features and features[0].is_sparse():
            sparse_attributes.append(attribute)
    for attribute in sparse_attributes:
        sparse_feature_sizes[attribute] = training_example.get_sparse_feature_sizes(
            attribute=attribute, featurizers=featurizers
        )
        if type:  # filter results by type
            sparse_feature_sizes[attribute] = {
                key: val
                for key, val in sparse_feature_sizes[attribute].items()
                if key == type
            }
    return sparse_feature_sizes


def get_tag_ids(
    example: Message, tag_spec: "EntityTagSpec", bilou_tagging: bool
) -> "Features":
    """Creates a feature array containing the entity tag ids of the given example.

    Args:
        example: the message
        tag_spec: entity tag spec
        bilou_tagging: indicates whether BILOU tagging should be used or not

    Returns:
        A list of features.
    """
    from rasa.nlu.test import determine_token_labels
    from rasa.nlu.utils.bilou_utils import bilou_tags_to_ids
    from rasa.shared.nlu.training_data.features import Features

    if bilou_tagging:
        _tags = bilou_tags_to_ids(example, tag_spec.tags_to_ids, tag_spec.tag_name)
    else:
        _tags = []
        for token in example.get(TOKENS_NAMES[TEXT]):
            _tag = determine_token_labels(
                token, example.get(ENTITIES), attribute_key=tag_spec.tag_name
            )
            _tags.append(tag_spec.tags_to_ids[_tag])

    # transpose to have seq_len x 1
    return Features(np.array([_tags]).T, IDS, tag_spec.tag_name, TAG_ID_ORIGIN)


def _surface_attributes(
    features: List[List[Dict[Text, List["Features"]]]],
    featurizers: Optional[List[Text]] = None,
) -> DefaultDict[Text, List[List[Optional[List["Features"]]]]]:
    """Restructure the input.

    "features" can, for example, be a dictionary of attributes (INTENT,
    TEXT, ACTION_NAME, ACTION_TEXT, ENTITIES, SLOTS, FORM) to a list of features for
    all dialogue turns in all training trackers.
    For NLU training it would just be a dictionary of attributes (either INTENT or
    RESPONSE, TEXT, and potentially ENTITIES) to a list of features for all training
    examples.

    The incoming "features" contain a dictionary as inner most value. This method
    surfaces this dictionary, so that it becomes the outer most value.

    Args:
        features: a dictionary of attributes to a list of features for all
            examples in the training data
        featurizers: the featurizers to consider

    Returns:
        A dictionary of attributes to a list of features for all examples.
    """
    # collect all attributes
    attributes = set(
        attribute
        for list_of_attribute_to_features in features
        for attribute_to_features in list_of_attribute_to_features
        for attribute in attribute_to_features.keys()
    )

    output = defaultdict(list)
    for list_of_attribute_to_features in features:
        intermediate_features = defaultdict(list)
        for attribute_to_features in list_of_attribute_to_features:
            for attribute in attributes:
                attribute_features = attribute_to_features.get(attribute)
                if featurizers:
                    attribute_features = _filter_features(
                        attribute_features, featurizers
                    )

                # if attribute is not present in the example, populate it with None
                intermediate_features[attribute].append(attribute_features)

        for key, collection_of_feature_collections in intermediate_features.items():
            output[key].append(collection_of_feature_collections)

    return output


def _filter_features(
    features: Optional[List["Features"]], featurizers: List[Text]
) -> Optional[List["Features"]]:
    """Filter the given features.

    Return only those features that are coming from one of the given featurizers.

    Args:
        features: list of features
        featurizers: names of featurizers to consider

    Returns:
        The filtered list of features.
    """
    if features is None or not featurizers:
        return features

    # it might be that the list of features also contains some tag_ids
    # the origin of the tag_ids is set to TAG_ID_ORIGIN
    # add TAG_ID_ORIGIN to the list of featurizers to make sure that we keep the
    # tag_ids
    featurizers.append(TAG_ID_ORIGIN)

    # filter the features
    return [f for f in features if f.origin in featurizers]


def _create_fake_features(
    all_features: List[List[List["Features"]]],
) -> List["Features"]:
    """Computes default feature values.

    All given features should have the same type, e.g. dense or sparse.

    Args:
        all_features: list containing all feature values encountered in the dataset
        for an attribute.

    Returns:
        The default features
    """
    example_features = next(
        iter(
            [
                list_of_features
                for list_of_list_of_features in all_features
                for list_of_features in list_of_list_of_features
                if list_of_features is not None
            ]
        )
    )

    # create fake_features for Nones
    fake_features = []
    for _features in example_features:
        new_features = copy.deepcopy(_features)
        if _features.is_dense():
            new_features.features = np.zeros(
                (0, _features.features.shape[-1]), _features.features.dtype
            )
        if _features.is_sparse():
            new_features.features = scipy.sparse.coo_matrix(
                (0, _features.features.shape[-1]), _features.features.dtype
            )
        fake_features.append(new_features)

    return fake_features


def convert_to_data_format(
    features: Union[
        List[List[Dict[Text, List["Features"]]]], List[Dict[Text, List["Features"]]]
    ],
    fake_features: Optional[Dict[Text, List["Features"]]] = None,
    consider_dialogue_dimension: bool = True,
    featurizers: Optional[List[Text]] = None,
) -> Tuple[Data, Optional[Dict[Text, List["Features"]]]]:
    """Converts the input into "Data" format.

    "features" can, for example, be a dictionary of attributes (INTENT,
    TEXT, ACTION_NAME, ACTION_TEXT, ENTITIES, SLOTS, FORM) to a list of features for
    all dialogue turns in all training trackers.
    For NLU training it would just be a dictionary of attributes (either INTENT or
    RESPONSE, TEXT, and potentially ENTITIES) to a list of features for all training
    examples.

    The "Data" format corresponds to Dict[Text, Dict[Text, List[FeatureArray]]]. It's
    a dictionary of attributes (e.g. TEXT) to a dictionary of secondary attributes
    (e.g. SEQUENCE or SENTENCE) to the list of actual features.

    Args:
        features: a dictionary of attributes to a list of features for all
            examples in the training data
        fake_features: Contains default feature values for attributes
        consider_dialogue_dimension: If set to false the dialogue dimension will be
            removed from the resulting sequence features.
        featurizers: the featurizers to consider

    Returns:
        Input in "Data" format and fake features
    """
    training = False
    if not fake_features:
        training = True
        fake_features = defaultdict(list)

    # unify format of incoming features
    if isinstance(features[0], Dict):
        features = [[dicts] for dicts in features]

    attribute_to_features = _surface_attributes(features, featurizers)

    attribute_data = {}

    # During prediction we need to iterate over the fake features attributes to

    # have all keys in the resulting model data
    if training:
        attributes = list(attribute_to_features.keys())
    else:
        attributes = list(fake_features.keys())

    # In case an attribute is not present during prediction, replace it with
    # None values that will then be replaced by fake features
    dialogue_length = 1
    num_examples = 1
    for _features in attribute_to_features.values():
        num_examples = max(num_examples, len(_features))
        dialogue_length = max(dialogue_length, len(_features[0]))
    absent_features = [[None] * dialogue_length] * num_examples

    for attribute in attributes:
        attribute_data[attribute] = _feature_arrays_for_attribute(
            attribute,
            absent_features,
            attribute_to_features,
            training,
            fake_features,
            consider_dialogue_dimension,
        )

    # ensure that all attributes are in the same order
    attribute_data = OrderedDict(sorted(attribute_data.items()))

    return attribute_data, fake_features


def _feature_arrays_for_attribute(
    attribute: Text,
    absent_features: List[Any],
    attribute_to_features: Dict[Text, List[List[List["Features"]]]],
    training: bool,
    fake_features: Dict[Text, List["Features"]],
    consider_dialogue_dimension: bool,
) -> Dict[Text, List[FeatureArray]]:
    """Create the features for the given attribute from the all examples features.

    Args:
        attribute: the attribute of Message to be featurized
        absent_features: list of Nones, used as features if `attribute_to_features`
            does not contain the `attribute`
        attribute_to_features: features for every example
        training: boolean indicating whether we are currently in training or not
        fake_features: zero features
        consider_dialogue_dimension: If set to false the dialogue dimension will be
          removed from the resulting sequence features.

    Returns:
        A dictionary of feature type to actual features for the given attribute.
    """
    features = (
        attribute_to_features[attribute]
        if attribute in attribute_to_features
        else absent_features
    )

    # in case some features for a specific attribute are
    # missing, replace them with a feature vector of zeros
    if training:
        fake_features[attribute] = _create_fake_features(features)

    (attribute_masks, _dense_features, _sparse_features) = _extract_features(
        features, fake_features[attribute], attribute
    )

    sparse_features = {}
    dense_features = {}

    for key, values in _sparse_features.items():
        if consider_dialogue_dimension:
            sparse_features[key] = FeatureArray(
                np.array(values), number_of_dimensions=4
            )
        else:
            sparse_features[key] = FeatureArray(
                np.array([v[0] for v in values]), number_of_dimensions=3
            )

    for key, values in _dense_features.items():
        if consider_dialogue_dimension:
            dense_features[key] = FeatureArray(np.array(values), number_of_dimensions=4)
        else:
            dense_features[key] = FeatureArray(
                np.array([v[0] for v in values]), number_of_dimensions=3
            )

    attribute_to_feature_arrays = {
        MASK: [FeatureArray(np.array(attribute_masks), number_of_dimensions=3)]
    }

    feature_types = set()
    feature_types.update(list(dense_features.keys()))
    feature_types.update(list(sparse_features.keys()))

    for feature_type in feature_types:
        attribute_to_feature_arrays[feature_type] = []
        if feature_type in sparse_features:
            attribute_to_feature_arrays[feature_type].append(
                sparse_features[feature_type]
            )
        if feature_type in dense_features:
            attribute_to_feature_arrays[feature_type].append(
                dense_features[feature_type]
            )

    return attribute_to_feature_arrays


def _extract_features(
    features: List[List[List["Features"]]],
    fake_features: List["Features"],
    attribute: Text,
) -> Tuple[
    List[np.ndarray],
    Dict[Text, List[List["Features"]]],
    Dict[Text, List[List["Features"]]],
]:
    """Create masks for all attributes of the given features and split the features
    into sparse and dense features.

    Args:
        features: all features
        fake_features: list of zero features

    Returns:
        - a list of attribute masks
        - a map of attribute to dense features
        - a map of attribute to sparse features
    """
    sparse_features = defaultdict(list)
    dense_features = defaultdict(list)
    attribute_masks = []

    for list_of_list_of_features in features:
        dialogue_sparse_features = defaultdict(list)
        dialogue_dense_features = defaultdict(list)

        # create a mask for every state
        # to capture which turn has which input
        attribute_mask = np.ones(len(list_of_list_of_features), np.float32)

        for i, list_of_features in enumerate(list_of_list_of_features):

            if list_of_features is None:
                # use zero features and set mask to zero
                attribute_mask[i] = 0
                list_of_features = fake_features

            for features in list_of_features:
                # in case of ENTITIES, if the attribute type matches either 'entity',
                # 'role', or 'group' the features correspond to the tag ids of that
                # entity type in order to distinguish later on between the different
                # tag ids, we use the entity type as key
                if attribute == ENTITIES and features.attribute in [
                    ENTITY_ATTRIBUTE_TYPE,
                    ENTITY_ATTRIBUTE_GROUP,
                    ENTITY_ATTRIBUTE_ROLE,
                ]:
                    key = features.attribute
                else:
                    key = features.type

                # all features should have the same types
                if features.is_sparse():
                    dialogue_sparse_features[key].append(features.features)
                else:
                    dialogue_dense_features[key].append(features.features)

        for key, value in dialogue_sparse_features.items():
            sparse_features[key].append(value)
        for key, value in dialogue_dense_features.items():
            dense_features[key].append(value)

        # add additional dimension to attribute mask
        # to get a vector of shape (dialogue length x 1),
        # the batch dim will be added later
        attribute_mask = np.expand_dims(attribute_mask, -1)
        attribute_masks.append(attribute_mask)

    return attribute_masks, dense_features, sparse_features
