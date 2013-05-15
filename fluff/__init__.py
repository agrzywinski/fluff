from couchdbkit import ResourceNotFound
from couchdbkit.ext.django import schema
import datetime
from dimagi.utils.decorators.memoized import memoized
from dimagi.utils.parsing import json_format_date
from fluff import exceptions
from pillowtop.listener import BasicPillow
from .signals import indicator_document_updated


def date_emitter(fn):
    fn._fluff_emitter = 'date'
    return fn


def null_emitter(fn):
    fn._fluff_emitter = 'null'
    return fn


def post_process(fn):
    fn._fluff_emitter = 'delayed'
    return fn


def filter_by(fn):
    fn._fluff_filter = True
    return fn


class CalculatorMeta(type):
    _counter = 0

    def __new__(mcs, name, bases, attrs):
        emitters = set()
        filters = set()

        parents = [p for p in bases if isinstance(p, CalculatorMeta)]
        for attr in attrs:
            if getattr(attrs[attr], '_fluff_emitter', None):
                emitters.add(attr)
            if getattr(attrs[attr], '_fluff_filter', False):
                filters.add(attr)

        # needs to inherit emitters and filters from all parents
        for parent in parents:
            emitters.update(parent._fluff_emitters)
            filters.update(parent._fluff_filters)

        cls = super(CalculatorMeta, mcs).__new__(mcs, name, bases, attrs)
        cls._fluff_emitters = emitters
        cls._fluff_filters = filters
        cls._counter = mcs._counter
        mcs._counter += 1
        return cls


class Calculator(object):
    __metaclass__ = CalculatorMeta

    window = None
    offsets = None

    # set by IndicatorDocumentMeta
    fluff = None
    slug = None

    # set by CalculatorMeta
    _fluff_emitters = None
    _fluff_filters = None

    def __init__(self, window=None):
        if window is not None:
            self.window = window
        self.offsets = self.offsets or {}
        emitters_not_covered = set(self._fluff_emitters)
        for emitter in set(self.offsets.keys() or {}) & emitters_not_covered:
            emitters_not_covered.remove(emitter)

        if self.window:
            emitters_not_covered.clear()

        if not isinstance(self.window, datetime.timedelta):
            # if window is set to None, for instance
            # fail here and not whenever that's run into below
            raise NotImplementedError(
                'window must be timedelta, not %s' % type(self.window))

        if emitters_not_covered:
            raise NotImplementedError(
                'The following emitters need to be added to your offsets, '
                'or you need to define a (default) window: %s' % (
                    ', '.join(emitters_not_covered)
                )
            )

    def filter(self, item):
        return True

    def to_python(self, value):
        return value

    def calculate(self, item):
        passes_filter = self.filter(item) and all(
            (getattr(self, slug)(item) for slug in self._fluff_filters)
        )
        values = {}
        for slug in self._fluff_emitters:
            emitter = getattr(self, slug)
            # 'delayed' is done with post-processing
            if emitter._fluff_emitter in ('date', 'null'):
                values[slug] = (
                    list(emitter(item))
                    if passes_filter else []
                )
        return values

    def get_window(self, now, emitter):
        if emitter in self.offsets:
            start, end = self.offsets[emitter]
            return now + start, now + end
        else:
            return now - self.window, now

    def get_result(self, key, reduce=True):
        return self.fluff.get_result(self.slug, key, reduce=reduce)


class FluffResult(object):
    def __init__(self, indicator_doc, calc_name, key):
        self.indicator_doc = indicator_doc
        self.calc_name = calc_name
        self.key = key

    @property
    @memoized
    def calculator(self):
        return self.indicator_doc.get_calculator(self.calc_name)

    def get(self, slug):
        return self._get(slug, reduce=True)

    def get_ids(self, slug):
        return self._get(slug, reduce=False)

    @memoized
    def _get(self, slug, reduce):
        doc_type = self.indicator_doc._doc_type
        shared_key = [doc_type] + self.key + [self.calc_name, slug]
        emitter = getattr(self.calculator, slug)
        emitter_type = emitter._fluff_emitter

        if emitter_type == 'date':
            now = datetime.datetime.utcnow().date()
            start, end = self.calculator.get_window(now, slug)
            q = self.indicator_doc.view(
                'fluff/generic',
                startkey=shared_key + [json_format_date(start)],
                endkey=shared_key + [json_format_date(end)],
                reduce=reduce,
            ).all()
        elif emitter_type == 'null':
            q = self.indicator_doc.view(
                'fluff/generic',
                key=shared_key + [None],
                reduce=reduce,
            ).all()
        elif emitter_type == 'delayed':
            ids = emitter(self)
            if reduce:
                return len(ids)
            else:
                return ids
        else:
            raise exceptions.EmitterTypeError(
                'emitter type %s not recognized' % emitter_type
            )

        if reduce:
            try:
                return q[0]['value']
            except IndexError:
                return 0
        else:
            def strip(id_string):
                prefix = '%s-' % self.indicator_doc.__name__
                assert id_string.startswith(prefix)
                return id_string[len(prefix):]
            return [strip(row['id']) for row in q]


class IndicatorDocumentMeta(schema.DocumentMeta):

    def __new__(mcs, name, bases, attrs):
        calculators = {}
        for attr_name, attr_value in attrs.items():
            if isinstance(attr_value, Calculator):
                calculators[attr_name] = attr_value
                attrs[attr_name] = schema.DictProperty()
        cls = super(IndicatorDocumentMeta, mcs).__new__(mcs, name, bases, attrs)
        if not hasattr(cls, '_calculators'):
            cls._calculators = {}
        cls._calculators.update(calculators)
        for slug, calculator in cls._calculators.items():
            calculator.fluff = cls
            calculator.slug = slug
        return cls


class IndicatorDocument(schema.Document):

    __metaclass__ = IndicatorDocumentMeta
    base_doc = 'IndicatorDocument'

    document_class = None
    group_by = ()

    def calculate(self, item):
        for attr, calculator in self._calculators.items():
            self[attr] = calculator.calculate(item)
        self.id = item.get_id
        for attr in self.group_by:
            self[attr] = item[attr]
        # overwrite whatever's in group_by with the default
        self['group_by'] = type(self)().group_by

    def diff(self, other_doc):
        """
        Get the diff between two IndicatorDocuments. Assumes that the documents are of the same type and that
        both have the same set of calculators and emitters. Doesn't support changes to group_by values.

        Return value is None for no diff or a dict with all indicator values
        that are different (added / removed / changed):
            {
                database: 'db1',
                doc_type: 'MyIndicators',
                group_names: ['domain', 'owner_id'],
                group_values: ['test', 'abc']
                indicator_changes: [
                    {
                    calculator: 'VistCalculator',
                    emitter: 'all_visits',
                    emitter_type: 'date',
                    values: ['2012-09-23', '2012-09-24']
                    },
                ]
            }
        """
        diff_keys = {}
        for calc_name in self._calculators.keys():
            if other_doc:
                calc_diff = self._shallow_dict_diff(self[calc_name], other_doc[calc_name])
                if calc_diff:
                    diff_keys[calc_name] = calc_diff
            else:
                for emitter_name, values in self[calc_name].items():
                    if values:
                        emitters = diff_keys.setdefault(calc_name, [])
                        emitters.append(emitter_name)

        if not diff_keys:
            return None

        diff = dict(database=self.Meta.app_label,
                    doc_type=self._doc_type,
                    group_names=list(self.group_by),
                    group_values=[self[calc_name] for calc_name in self.group_by],
                    indicator_changes=[])
        indicators = diff["indicator_changes"]

        for calc_name, emitter_names in diff_keys.items():
            indicators.extend(self._indicator_diff(calc_name, emitter_names, other_doc))

        return diff

    def _indicator_diff(self, calc_name, emitter_names, other_doc):
        indicators = []
        for emitter_name in emitter_names:
                if other_doc:
                    values_diff = list(set(self[calc_name][emitter_name]) - set(other_doc[calc_name][emitter_name]))
                else:
                    values_diff = self[calc_name][emitter_name]

                emitter_type = getattr(self._calculators[calc_name], emitter_name)._fluff_emitter
                indicators.append(dict(calculator=calc_name,
                                       emitter=emitter_name,
                                       emitter_type=emitter_type,
                                       values=values_diff))
        return indicators

    def _shallow_dict_diff(self, left, right):
        if not left and not right:
            return None
        elif not left or not right:
            return left.keys() if left else right.keys()

        left_set, right_set = set(left.keys()), set(right.keys())
        intersect = right_set.intersection(left_set)

        added = right_set - intersect
        removed = left_set - intersect
        changed = set(o for o in intersect if left[o] != right[o])
        return added | removed | changed

    @classmethod
    def pillow(cls):
        doc_type = cls.document_class._doc_type
        domains = ' '.join(cls.domains)
        return type(FluffPillow)(cls.__name__ + 'Pillow', (FluffPillow,), {
            'couch_filter': 'fluff_filter/domain_type',
            'extra_args': {
                'domains': domains,
                'doc_type': doc_type
            },
            'document_class': cls.document_class,
            'indicator_class': cls,
        })

    @classmethod
    def has_calculator(cls, calc_name):
        return calc_name in cls._calculators

    @classmethod
    def get_calculator(cls, calc_name):
        return cls._calculators[calc_name]

    @classmethod
    def get_result(cls, calc_name, key, reduce=True):
        print cls, calc_name, key, reduce
        result = FluffResult(cls, calc_name, key)
        serialized = {}
        for emitter_name in result.calculator._fluff_emitters:
            serialized[emitter_name] = result._get(emitter_name, reduce=reduce)
        return serialized

    class Meta:
        app_label = 'fluff'


class FluffPillow(BasicPillow):
    indicator_class = IndicatorDocument

    def change_transform(self, doc_dict):
        doc = self.document_class.wrap(doc_dict)
        indicator_id = '%s-%s' % (self.indicator_class.__name__, doc.get_id)

        try:
            current_indicator = self.indicator_class.get(indicator_id)
        except ResourceNotFound:
            current_indicator = None

        if not current_indicator:
            indicator = self.indicator_class(_id=indicator_id)
        else:
            indicator = current_indicator
            current_indicator = indicator.to_json()
        indicator.calculate(doc)
        return current_indicator, indicator

    def change_transport(self, indicators):
        old_indicator, new_indicator = indicators
        new_indicator.save()

        diff = new_indicator.diff(old_indicator)
        if diff:
            indicator_document_updated.send(sender=self, diff=diff)
