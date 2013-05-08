"""
Common Functionality for support of the Query API
"""

import urlparse
import urllib

from indivo.lib.sharing_utils import carenet_facts_filter
from indivo.lib.utils import render_template
from indivo.lib.iso8601 import parse_iso8601_datetime
from django.db.models import Avg, Count, Max, Min, Sum
from django.db import connection
from django.db.backends import postgresql_psycopg2, mysql, oracle
from django.db.models.query import QuerySet


db_string = connection.settings_dict['ENGINE']
if '.' in db_string:
    db_module, db_name = db_string.rsplit('.', 1)
    DB_ENGINE = getattr(__import__(db_module, fromlist=[db_name]), db_name)
else:
    DB_ENGINE = __import__(db_string)

DATE = 'date'
STRING = 'string'
NUMBER = 'number'

EXPOSED_TYPES = {
  STRING: str,
  DATE: parse_iso8601_datetime,
  NUMBER: float
  }

AGG_OPS = {
'sum': (Sum, [NUMBER]),
'avg': (Avg, [NUMBER]),
'max': (Max, [NUMBER, DATE]),
'min': (Min, [NUMBER, DATE]),
'count': (Count, [NUMBER, DATE, STRING])
}

TIME_INCRS = {
    'hour': {postgresql_psycopg2:'YYYY-MM-DD-HH24',
             oracle:'YYYY-MM-DD-HH24',
             mysql:'%%Y-%%m-%%d-%%H',},
    'day': {postgresql_psycopg2:'YYYY-MM-DD',
            oracle:'YYYY-MM-DD',
            mysql:'%%Y-%%m-%%d',},
    'week': {postgresql_psycopg2:'YYYY-WW',
             oracle:'YYYY-WW',
             mysql:'%%Y-%%U',},
    'month': {postgresql_psycopg2:'YYYY-MM',
              oracle:'YYYY-MM',
              mysql:'%%Y-%%m',},
    'year': {postgresql_psycopg2:'YYYY',
             oracle:'YYYY',
             mysql:'%%Y',},
    'hourofday': {postgresql_psycopg2:'HH24',
                  oracle:'HH24',
                  mysql:'%%H',},
    'dayofweek': {postgresql_psycopg2:'D',
                  oracle:'D',
                  mysql:'%%w',},
    'weekofyear': {postgresql_psycopg2:'WW',
                   oracle:'WW',
                   mysql:'%%U',},
    'monthofyear': {postgresql_psycopg2:'MM',
                    oracle:'MM',
                    mysql:'%%m',},
    }

FORMAT_STRS = {
    postgresql_psycopg2: "to_char(\"%(field)s\", '%(format)s')",
    oracle: "to_char(%(field)s, '%(format)s')",
    mysql: "date_format(%(field)s, '%(format)s')",
}

OUTPUT_TEMPLATE = 'reports/report'
AGGREGATE_TEMPLATE = 'reports/aggregate.xml'

RELATED_LIST = [
    'document',
    'document__creator',
    'document__creator__account',
    'document__creator__pha',
    'document__suppressed_by',
    'document__suppressed_by__account',
    'document__status',
    ]

class FactQuery(object):
    def __init__(self, model, model_filters,
                 query_options,
                 record=None, 
                 carenet=None,
                 fact_id=None,
                 request_url=None
                 ):
        self.model = model
        self.valid_filters = model_filters
        self.group_by = query_options.get('group_by')
        self.date_group = query_options.get('date_group')
        self.aggregate_by = query_options.get('aggregate_by')
        self.limit = query_options.get('limit')
        self.offset = query_options.get('offset')
        self.offset = self.offset if self.offset >=0 else 0
        self.order_by = query_options.get('order_by')
        self.status = query_options.get('status')
        self.date_range = query_options.get('date_range')
        self.query_filters = query_options.get('filters')
        
        self.results = None
        self.trc = None
        self.aggregate_p = None
        self.grouping_p = None
        self.flat_aggregation = None

        self.carenet = carenet
        self.record = carenet.record if carenet else record
        
        self.fact_id = fact_id
        
        # keep track of request url for handling nextPageURL in SMART requests
        self.request_url = request_url

    def render(self, item_template, output_template=OUTPUT_TEMPLATE):
        if self.results is None:
            self.execute()

        if self.aggregate_by:
            item_template = AGGREGATE_TEMPLATE
            
        # if we can, iterate efficiently over our results
        if hasattr(self.results, 'iterator'):
            results = self.results.iterator()
        else:
            results = self.results

        template_args = {'fobjs': results,
                         'trc': self.trc,
                         'group_by': self.group_by, 
                         'date_group': self.date_group, 
                         'aggregate_by': self.aggregate_by,
                         'limit': self.limit, 
                         'offset': self.offset,
                         'order_by': self.order_by,
                         'status': self.status,
                         'date_range': self.date_range, 
                         'filters': self.query_filters,        
                         'item_template': item_template
                         }
        return render_template(output_template, template_args, type="xml")

    def execute(self):
        '''
        New API Query Interface (to be released for Beta 3)
        Query operators are evaluated as follows:
        1. filter operators, including date_range but excluding limit and offset, are applied first.
        2. Group_by and date_group, if supplied, are evaluated next
        3. Aggregate by is evaluated
        4. order_by is applied
        5. We evaluate the query to get an ordered list of results, the apply limit and offset.
        '''

        if self.fact_id:
            # query for a specific instance
            # we use .filter here instead of .get_object_or_404 so we have a QuerySet 
            results = self.model.objects.filter(id=self.fact_id)
            
            # using len() instead of .count() since it is a result set of 1 and 
            # we don't worry about delaying execution or storing lots of data in-memory
            self.trc = len(results)  
        else:
            # This is okay, Django evaluates lazily
            results = self.model.objects.all()
    
            # Apply select_related for performance here
            results = results.select_related(*RELATED_LIST)
    
            # 1. Apply filter operators (but not limit/offset).
            results = self._apply_filters(results)
    
            # 2. Evaluate group_by or date_group
            results = self._apply_grouping(results)
    
            # 3. Evaluate aggregate_by
            self.grouping_p = self.group_by or self.date_group
            self.flat_aggregation = self.aggregate_by and not self.grouping_p
            results = self._apply_aggregation(results)
    
            # 4. Order_by
            # ignore order_by if we have a single aggregation    
            if not self.flat_aggregation:
                results = self._apply_ordering(results)
        
            # 5. limit and offset. Make sure to grab the total result count
            # before paging is applied and we lose it.
    
            # No need to get the count or worry about paging for a flat
            # aggregation, which was already evaluated
            if self.flat_aggregation:
                self.trc = 1
                results = [results] # [{'aggregation': 'value'}]
    
            # Avoid evaluation for as long as possible: pass back a QuerySet object
            else:
                self.trc = results.count()
                if self.limit:
                    results = results[self.offset:self.offset+self.limit]
                
        # And we're done!
        self.results = results

    def next_url(self):
        next_url = None
        
        # to accommodate merged QuerySets used by SMART Allergies, which might be lists
        if isinstance(self.results, QuerySet):
            result_count = self.results.count()
        else:
            result_count = len(self.results)
        
        if self.limit and (self.trc > (self.offset + result_count)):
            parsed_uri = urlparse.urlparse(self.request_url)
            if parsed_uri.query:
                query_dict = dict(urlparse.parse_qsl(parsed_uri.query))
                query_dict['offset'] = self.offset + self.limit
                parsed_uri = list(parsed_uri)
                parsed_uri[4] = urllib.urlencode(query_dict)
                next_url = urlparse.urlunparse(parsed_uri)

        return next_url

    def _apply_filters(self, results):
        # Carenet filters.
        # DH 04-07-2011: Moved up front and changed to not evaluate the queryset

        # Need to allow queries with no record or carenet, i.e., Audit, which isn't constrained to a single record
        if self.record:
            results = results.filter(record=self.record)
        results = carenet_facts_filter(self.carenet, results)


        filter_args = {}
        for field, val in self.query_filters.iteritems():
            if self.valid_filters.has_key(field):
                field_type = self.valid_filters[field][1]
                try:
                    val = val.split('|')
                    if len(val) == 1:
                        parsed_val = EXPOSED_TYPES[field_type](val[0])
                        filter_args[self.valid_filters[field][0]] = parsed_val
                    else:
                        parsed_values = [EXPOSED_TYPES[field_type](x) for x in val]
                        if len(parsed_values) > 0:
                            filter_args[self.valid_filters[field][0] + '__in'] = parsed_values
                except:
                    raise ValueError('Invalid argument type for field %s: expected %s, got %s'%(field, field_type, val))
            else:
                raise ValueError('Invalid filter for fact type %s: %s'%(self.model.__name__, field))
  
        if self.date_range:
            if self.valid_filters.has_key(self.date_range['field']):
                field_type = self.valid_filters[self.date_range['field']][1]
                if field_type != DATE:
                    raise ValueError('Date Ranges may only be calculated over fields of type "date": got %s(%s)'%(self.date_range['field'], field_type))

                if self.date_range['start_date']:
                    filter_args['%s__gte'%(self.valid_filters[self.date_range['field']][0])] = self.date_range['start_date']
                if self.date_range['end_date']:
                    filter_args['%s__lte'%(self.valid_filters[self.date_range['field']][0])] = self.date_range['end_date']
            else:
                raise ValueError('Invalid date range filter for fact type %s: %s'%(self.model.__name__, self.date_range['field']))

        if self.status:
            filter_args['document__status'] = self.status

        if filter_args:
            results = results.filter(**filter_args)

        # Results look like:
        # [obj1, obj2, ...] For every Fact object we haven't filtered out
        return results

    def _apply_grouping(self, results):
        group_field = 'all'

        # Handle the ordinary group
        if self.group_by:
            if self.valid_filters.has_key(self.group_by):          
                group_field = self.valid_filters[self.group_by][0]
            else:
                raise ValueError('Invalid grouping field for fact type %s: %s'%(self.model.__name__, self.group_by))

        # Handle the date group
        elif self.date_group:
            if self.valid_filters.has_key(self.date_group['field']):
                field_type = self.valid_filters[self.date_group['field']][1]
                if field_type != DATE:
                    raise ValueError('Date groups may only be calculated over fields of type "date": got %s(%s)'%(self.date_group['field'], self.field_type))

                group_field = self.valid_filters[self.date_group['field']][0]
                date_incr = self.date_group['time_incr']
                if TIME_INCRS.has_key(date_incr):
                    time_format = TIME_INCRS[date_incr][DB_ENGINE]
                    format_str = FORMAT_STRS[DB_ENGINE]
                    results = results.extra(select={date_incr:format_str%{"field":group_field, "format":time_format}})

                    # From now on, we look at the date-formatted string only
                    group_field = date_incr
                else:
                    raise ValueError('Invalid date_group Increment: %s'%(date_incr))
            else:
                raise ValueError('Invalid grouping field for fact type %s: %s'%(self.model.__name__, self.date_group['field']))
    
        if group_field is not 'all':
            results = results.values(group_field)

        # Results look like:
        # [{'group_field': 'value1'}, {'group_field': 'value2'}], 1 dict per Fact object if we grouped
        # if there was no grouping, results look like: [obj1, obj2, ...]
        return results

    def _apply_aggregation(self, results):
        if self.aggregate_by:      
            agg_field = self.aggregate_by['field']
            if self.valid_filters.has_key(agg_field):
                agg_field_type = self.valid_filters[agg_field][1]

                # Look up the operator
                if AGG_OPS.has_key(self.aggregate_by['operator']):    
                    agg = AGG_OPS[self.aggregate_by['operator']]
                    agg_func_types = agg[1]
                    if agg_field_type not in agg_func_types:
                        raise ValueError('Cannot apply aggregate function %s (type %s) to field %s (type %s)'%(self.aggregate_by['operator'], agg_func_types, agg_field, agg_field_type))

                    agg_func = agg[0]
                    agg_args = { 'aggregate_value': agg_func(self.valid_filters[agg_field][0])}
                else:
                    raise ValueError('Invalid aggregation operator: %s'%(self.aggregate_by['operator']))

                # If we grouped, handle differently
                if self.grouping_p:
                    results = results.annotate(**agg_args)
                else:
                    results = results.aggregate(**agg_args)
            else:
                raise ValueError('Invalid aggregation field for fact type %s: %s'%(self.model.__name__, agg_field))
        else:
            if self.grouping_p:
                raise ValueError('Cannot make grouped queries without specifying an Aggregation!')

        # Results look like:
        # [{'group_field' : value1, 'aggregation': agg_value} ...] 1 dict per each UNIQUE group_field value
        # If there was no grouping, results look like: {'aggregation': value'}
        # If there was no grouping OR aggregation, results look like: [obj1, obj2...]    
        return results

    def _apply_ordering(self, results):
        if self.order_by:
            desc = self.order_by[0] == '-'
            order_by_field_ext = self.order_by if not desc else self.order_by[1:]

            # get the internal model field for order by
            if self.valid_filters.has_key(order_by_field_ext):
                order_by_field = self.valid_filters[order_by_field_ext][0]
            else:
                raise ValueError('Invalid order by field for fact type %s: %s'%(self.model.__name__, self.order_by))

            # Handle special cases of aggregation and grouping
            if self.aggregate_by and order_by_field_ext == self.aggregate_by['field']:
                order_by_field = 'aggregate_value'
            elif self.group_by and order_by_field_ext != self.group_by:
                raise ValueError('OrderBy fields in aggregations may only refer to the grouping field or the aggregation field. Your field was: %s'%(self.order_by))
            elif self.date_group and order_by_field_ext != self.date_group['field']:
                raise ValueError('OrderBy fields in aggregations may only refer to the grouping field or the aggregation field. Your field was: %s'%(self.order_by))
            elif self.date_group:
                order_by_field = self.date_group['time_incr']

            # Django seems to be nondeterministic in its ordering of ties, so let's add an implicit secondary ordering on primary key
            secondary_order_by = 'id'

            # Do the ordering
            order_by_str = order_by_field if not desc else '-'+order_by_field
            results = results.order_by(order_by_str, secondary_order_by)    
        else:
            # Clear ordering if none was specified, to avoid bad interactions with grouping
            results = results.order_by()

        return results
