import graphene
from jet_bridge_base.filters import lookups
from jet_bridge_base.filters.filter_for_dbfield import filter_for_data_type
from jet_bridge_base.serializers.model_serializer import get_column_data_type
from sqlalchemy import inspect, desc, column as sqlcolumn

from jet_bridge_base.db import get_mapped_base
from jet_bridge_base.responses.json import JSONResponse
from jet_bridge_base.utils.queryset import queryset_count_optimized
from jet_bridge_base.views.base.api import APIView


class RawScalar(graphene.Scalar):
    @staticmethod
    def serialize(value):
        return value

    @staticmethod
    def parse_literal(node, _variables=None):
        return node.value

    @staticmethod
    def parse_value(value):
        return value


class StringFiltersType(graphene.InputObjectType):
    eq = graphene.String()
    lt = graphene.String()
    lte = graphene.String()
    gt = graphene.String()
    gte = graphene.String()
    in_op = graphene.List(graphene.String, name='in')
    containsI = graphene.String()
    isNull = graphene.String()
    startsWithI = graphene.String()
    endsWithI = graphene.String()
    and_op = graphene.String(name='and')
    or_op = graphene.String(name='or')
    not_op = graphene.String(name='not')


class PaginationType(graphene.InputObjectType):
    page = graphene.Int()
    offset = graphene.Int()
    limit = graphene.Int()


class PaginationResponseType(graphene.ObjectType):
    count = graphene.Int()
    limit = graphene.Int()
    offset = graphene.Int(required=False)
    page = graphene.Int(required=False)
    hasMore = graphene.Boolean(required=False)


class GraphQLView(APIView):
    # serializer_class = ModelDescriptionSerializer
    # permission_classes = (HasProjectPermissions,)

    def get_queryset(self, request, Model):
        queryset = request.session.query(Model)

        mapper = inspect(Model)
        auto_pk = getattr(mapper.tables[0], '__jet_auto_pk__', False) if len(mapper.tables) else None
        if auto_pk:
            queryset = queryset.filter(mapper.primary_key[0].isnot(None))

        return queryset

    def get_model_filters_type(self, mapper, depth=1):
        name = mapper.selectable.name
        filter_attrs = {}

        for column in mapper.columns:
            if column.foreign_keys and depth <= 2 and column.name == 'owner_id':
                foreign_key = next(iter(column.foreign_keys))
                table = foreign_key.column.table

                RelatedTableFiltersType = self.get_model_filters_type(table, depth + 1)
                RelationFiltersType = type('Model{}{}RelationFiltersType_{}'.format(name, column.name, depth), (graphene.InputObjectType,), {
                   'eq': graphene.String(),
                   'relation': RelatedTableFiltersType()
                })
                filter_attrs[column.name] = RelationFiltersType()
            else:
                filter_attrs[column.name] = StringFiltersType()

        return type('Model{}FiltersType_{}'.format(name, depth), (graphene.InputObjectType,), filter_attrs)

    def filter_queryset(self, queryset, mapper, filters, relationship=None):
        columns = dict(map(lambda x: (x.key, x), mapper.columns))

        for filter_name, filter_lookups in filters.items():
            column = columns.get(filter_name)

            if column is None:
                continue

            for lookup_name, lookup_value in filter_lookups.items():
                if lookup_name == 'relation':
                    foreign_key = next(iter(column.foreign_keys))
                    relation_table = foreign_key.column.table
                    relationship = None

                    for relation in mapper.relationships.values():
                        if len(relation.local_columns) != 1:
                            continue
                        local_column = next(iter(relation.local_columns))
                        if local_column is None:
                            continue
                        if local_column.name != column.name:
                            continue
                        relationship = relation.class_attribute
                        break

                    if relationship:
                        queryset = self.filter_queryset(queryset, relation_table, lookup_value, relationship)
                else:
                    item = filter_for_data_type(column.type)
                    lookup = lookups.by_gql(lookup_name)
                    instance = item['filter_class'](
                        name=column.key,
                        column=column,
                        lookup=lookup,
                        exclude=False
                    )
                    criterion = instance.get_loookup_criterion(lookup_value)

                    if relationship:
                        queryset = queryset.filter(relationship.has(criterion))
                    else:
                        queryset = queryset.filter(criterion)

        return queryset

    def sort_queryset(self, queryset, sort):
        def map_order_field(sorting):
            parts = sorting.split(':', 1)

            if len(parts) == 2:
                name = parts[0]
                descending = parts[1] == 'desc'
            else:
                name = parts[0]
                descending = False

            field = sqlcolumn(name)
            if descending:
                field = desc(field)
            return field

        if len(sort):
            order_by = list(map(lambda x: map_order_field(x), sort))
            queryset = queryset.order_by(*order_by)

        return queryset

    def get_pagination_limit(self, pagination):
        return pagination.get('limit', 20)

    def paginate_queryset(self, queryset, pagination):
        limit = self.get_pagination_limit(pagination)

        if 'offset' in pagination:
            queryset = queryset.offset(pagination['offset'])
        elif 'page' in pagination:
            queryset = queryset.offset((pagination['page'] - 1) * limit)

        queryset = queryset.limit(limit)

        return queryset

    def get_model_attrs_type(self, mapper):
        name = mapper.selectable.name

        def create_field_resolver(column):
            def resolver(parent, info):
                value = getattr(parent, column.name)
                data_type = get_column_data_type(column)
                field = data_type()
                return field.to_representation(value)

            return resolver

        record_attrs = {}

        for column in mapper.columns:
            record_attrs[column.name] = RawScalar()
            record_attrs['resolve_{}'.format(column.name)] = create_field_resolver(column)

        return type('Model{}RecordAttrsType'.format(name), (graphene.ObjectType,), record_attrs)

    def get_query_type(self, request):
        MappedBase = get_mapped_base(request)

        query_attrs = {}

        for Model in MappedBase.classes:
            mapper = inspect(Model)
            name = mapper.selectable.name

            FiltersType = self.get_model_filters_type(mapper)
            ModelAttrsType = self.get_model_attrs_type(mapper)
            ModelType = type('Model{}ModelType'.format(name), (graphene.ObjectType,), {
                'attrs': graphene.Field(ModelAttrsType)
            })
            ModelListType = type('Model{}ModelListType'.format(name), (graphene.ObjectType,), {
                'data': graphene.List(ModelType),
                'pagination': graphene.Field(PaginationResponseType)
            })

            def create_list_resolver(Model, mapper):
                def resolver(parent, info, filters=None, sort=None, pagination=None):
                    try:
                        filters = filters or {}
                        sort = sort or []
                        pagination = pagination or {}
                        queryset = self.get_queryset(request, Model)

                        queryset = self.filter_queryset(queryset, mapper, filters)
                        queryset = self.sort_queryset(queryset, sort)

                        queryset_page = self.paginate_queryset(queryset, pagination)

                        result = {
                            'data': list(map(lambda x: {
                                'attrs': x
                            }, queryset_page))
                        }

                        for selection in info.field_asts[0].selection_set.selections:
                            if selection.name.value == 'pagination':
                                count = queryset_count_optimized(request, queryset)
                                limit = self.get_pagination_limit(pagination)
                                offset = pagination.get('offset')
                                page = pagination.get('page')

                                result['pagination'] = {
                                    'count': count,
                                    'limit': limit,
                                    'offset': offset,
                                    'page': page
                                }

                                if offset is not None:
                                    result['pagination']['hasMore'] = offset + limit < count
                                elif page is not None:
                                    result['pagination']['hasMore'] = page * limit < count

                        return result
                    except Exception as e:
                        raise e
                return resolver

            query_attrs[name] = graphene.Field(
                ModelListType,
                filters=FiltersType(),
                sort=graphene.List(graphene.String),
                pagination=PaginationType()
            )
            query_attrs['resolve_{}'.format(name)] = create_list_resolver(Model, mapper)

        return type('Query', (graphene.ObjectType,), query_attrs)

    def get(self, request, *args, **kwargs):
        return self.post(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        Query = self.get_query_type(request)

        schema = graphene.Schema(query=Query, auto_camelcase=False)

        if 'query' not in request.data:
            return JSONResponse({})

        query = request.data.get('query')
        result = schema.execute(query, variables={}, context_value={'session': request.session})

        if result.invalid:
            return JSONResponse({'errors': map(lambda x: x.message, result.errors)})

        return JSONResponse({
            'data': result.data
        })
