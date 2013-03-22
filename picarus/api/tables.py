import base64
import annotators
import bottle
import json
import hadoopy_hbase
import time
import uuid
import hashlib
import mturk_vision
import picarus.api
import numpy as np
import crawlers
import re
import picarus_takeout
import msgpack # TODO: Abstract these operations
from driver import PicarusManager
from picarus._importer import call_import
from flickr_keys import FLICKR_API_KEY, FLICKR_API_SECRET
from parameters import PARAM_SCHEMAS_SERVE

# These need to be set before using this module
thrift_lock = None
VERSION = None
ANNOTATORS = None


class PrettyFloat(float):
    def __repr__(self):
        return '%.15g' % self


def dod_to_lod_b64(dod):
    # Converts from dod[row][column] to list of {row, col0_ub64:val0_b64, ...}
    # dod: dict of dicts
    # lod: list of dicts
    outs = []
    for row, columns in sorted(dod.items(), key=lambda x: x[0]):
        out = {'row': base64.urlsafe_b64encode(row)}
        out.update({base64.urlsafe_b64encode(x): base64.b64encode(y) if isinstance(y, str) else base64.b64encode(json.dumps(y)) for x, y in columns.items()})
        outs.append(out)
    return outs

PARAM_SCHEMAS_B64 = dod_to_lod_b64(PARAM_SCHEMAS_SERVE)


def encode_row(row, columns):
    out = {base64.urlsafe_b64encode(k): base64.b64encode(v) for k, v in columns.items()}
    out['row'] = base64.urlsafe_b64encode(row)
    return out


def _takeout_model_link_from_key(manager, key):
    model, columns = manager.key_to_model(key)
    if not isinstance(model, dict) or model['name'] not in ('picarus.HistogramImageFeature', 'picarus.ImagePreprocessor', 'picarus.LinearClassifier'):
        bottle.abort(400)
    return base64.urlsafe_b64decode(columns['input']), model


def _takeout_model_chain_from_key(manager, key):
    model, columns = manager.key_to_model(key)
    if columns['input_type'] == 'raw_image':
        return [_takeout_model_link_from_key(manager, key)]
    return _takeout_model_chain_from_key(manager, base64.urlsafe_b64decode(columns['input'])) + [_takeout_model_link_from_key(manager, key)]


def _parse_params(params, schema):
    kw = {}
    schema_params = schema['params']
    prefix = 'param'
    get_param = lambda x: params[prefix + '-' + x]
    for param_name, param in schema_params.items():
        if param['type'] == 'enum':
            param_value = get_param(param_name)
            if param_value not in param['values']:
                bottle.abort(400)
            kw[param_name] = param_value
        elif param['type'] == 'int':
            param_value = int(get_param(param_name))
            if not (param['min'] <= param_value < param['max']):
                bottle.abort(400)
            kw[param_name] = param_value
        elif param['type'] == 'const':
            kw[param_name] = param['value']
        elif param['type'] == 'str':
            kw[param_name] = get_param(param_name)
        else:
            bottle.abort(400)
    return kw


def _get_input(params, key):
    # TODO: Verify that model keys exist
    return params['input-' + key]


def _create_model_from_params(manager, email, path, params):
    try:
        schema = PARAM_SCHEMAS_SERVE[path]
        model_params = _parse_params(params, schema)
        model = {'name': schema['name'], 'kw': model_params}
        input = _get_input(params, schema['input_type'])
        row = manager.input_model_param_to_key(input=input, model=model, input_type=schema['input_type'], output_type=schema['output_type'], email=email, name=manager.model_to_name(model))
        return {'row': base64.urlsafe_b64encode(row)}
    except ValueError:
        bottle.abort(500)


def _create_model_from_factory(manager, email, path, create_model, params):
    schema = PARAM_SCHEMAS_SERVE[path]
    model_params = _parse_params(params, schema)
    inputs = {x: _get_input(params, x) for x in schema['input_types']}
    row = manager.input_model_param_to_key(**create_model(model_params, inputs, schema))
    return {'row': base64.urlsafe_b64encode(row)}


def _user_to_dict(user):
    cols = {'stats': json.dumps(user.stats()), 'upload_row_prefix': user.upload_row_prefix, 'image_prefixes': json.dumps(user.image_prefixes)}
    cols = {base64.urlsafe_b64encode(x) : base64.b64encode(y) for x, y in cols.items()}
    cols['row'] = base64.urlsafe_b64encode(user.email)
    return cols


class BaseTableSmall(object):
    """Base class for tables that easily fit in memory"""

    def __init__(self):
        pass

    def get_table(self, columns):
        bottle.response.headers["Content-type"] = "application/json"
        columns = set(map(base64.urlsafe_b64encode, columns))
        full_table = self._get_table()
        if columns:
            columns.add('row')
            return json.dumps([{y: x[y] for y in columns.intersection(x)} for x in full_table])
        else:
            return json.dumps(full_table)

    def get_row(self, row, columns):
        table = self._get_row(row, columns)
        column_values = table[row]
        if columns:
            columns = set(columns).intersection(column_values)
            return {base64.urlsafe_b64encode(x): base64.b64encode(column_values[x])
                    for x in columns}
        else:
            return {base64.urlsafe_b64encode(x): base64.b64encode(y)
                    for x, y in column_values.items()}


class ParametersTable(BaseTableSmall):

    def __init__(self):
        self._params = dod_to_lod_b64(PARAM_SCHEMAS_SERVE)

    def _get_table(self):
        return self._params


class PrefixesTable(BaseTableSmall):

    def __init__(self, _auth_user):
        self._prefixes = {'images': _auth_user.image_prefixes}
        self._auth_user = _auth_user

    def _get_table(self):
        return dod_to_lod_b64(self._prefixes)

    def _get_row(self, row, columns):
        return self._prefixes

    def _row_column_value_validator(self, table, prefix, permissions):
        if permissions not in ('r', 'rw'):
            bottle.abort(403)
        for cur_prefix, cur_permissions in self._prefixes[table].items():
            if prefix.startswith(cur_prefix) and cur_permissions.startswith(permissions):
                return
        bottle.abort(403)  # No valid prefix lets the user do this

    def patch_row(self, row, params, files):
        if files:
            bottle.abort(403)  # Files not allowed
        for x, y in params.items():
            new_prefix = base64.urlsafe_b64decode(x)
            new_permissions = base64.b64decode(y)
            self._row_column_value_validator(row, new_prefix, new_permissions)
            if row == 'images':
                self._auth_user.add_image_prefix(new_prefix, new_permissions)
            else:
                bottle.abort(403)
        return {}

    def delete_column(self, row, column):
        if row == 'images':
            self._auth_user.remove_image_prefix(column)
        else:
            bottle.abort(403)
        return {}


class UsersTable(object):
    # TODO: Remove the user's table, it is not necessary anymore

    def __init__(self, _auth_user):
        self._auth_user = _auth_user

    def get_row(self, row, columns):
        if self._auth_user.email != row:
            bottle.abort(401)
        return _user_to_dict(self._auth_user)


class AnnotationsTable(BaseTableSmall):

    def __init__(self, _auth_user):
        self.owner = _auth_user.email

    def _get_table(self):
        try:
            cur_table = ANNOTATORS.get_tasks(self.owner)
        except annotators.UnauthorizedException:
            bottle.abort(401)
        return dod_to_lod_b64(cur_table)

    def delete_row(self, row):
        ANNOTATORS.delete_task(row, self.owner)
        return {}


class AnnotationDataTable(BaseTableSmall):

    def __init__(self, _auth_user, table, task):
        self.owner = _auth_user.email
        self.table = table
        self.task = task

    def _get_table(self):
        try:
            secret = ANNOTATORS.get_task_secret(self.task, self.owner)
        except annotators.UnauthorizedException:
            bottle.abort(401)
        if self.table == 'results':
            table = ANNOTATORS.get_manager(self.task).admin_results(secret)
        elif self.table == 'users':
            table = ANNOTATORS.get_manager(self.task).admin_users(secret)
        else:
            bottle.abort(500)
        return dod_to_lod_b64(table)


class HBaseTable(object):

    def __init__(self, _auth_user, table):
        self.owner = _auth_user.email
        self.table = table

    def patch_row(self, row, params, files):
        with thrift_lock() as thrift:
            self._row_validate(row, 'rw', thrift)
            mutations = []
            for x, y in files.items():
                cur_column = base64.urlsafe_b64decode(x)
                self._column_write_validate(cur_column)
                thrift.mutateRow(self.table, row, [hadoopy_hbase.Mutation(column=cur_column, value=y.file.read())])
            for x, y in params.items():
                cur_column = base64.urlsafe_b64decode(x)
                self._column_write_validate(cur_column)
                mutations.append(hadoopy_hbase.Mutation(column=cur_column, value=base64.b64decode(y)))
            if mutations:
                thrift.mutateRow(self.table, row, mutations)
        return {}

    def delete_row(self, row):
        with thrift_lock() as thrift:
            self._row_validate(row, 'rw', thrift)
            thrift.deleteAllRow(self.table, row)
            return {}

    def delete_column(self, row, column):
        with thrift_lock() as thrift:
            self._row_validate(row, 'rw', thrift)
            thrift.mutateRow(self.table, row, [hadoopy_hbase.Mutation(column=column, isDelete=True)])
            return {}


class ImagesHBaseTable(HBaseTable):

    def __init__(self, _auth_user):
        super(ImagesHBaseTable, self).__init__(_auth_user, 'images')
        self.image_prefixes = _auth_user.image_prefixes
        self.upload_row_prefix = _auth_user.upload_row_prefix

    def _slice_validate(self, start_row, stop_row, permissions):
        prefixes = self.image_prefixes
        permissions = set(permissions)
        prefixes = [x for x, y in prefixes.items() if set(y).issuperset(permissions)]
        for prefix in prefixes:
            if prefix == '':
                return
            # NOTE: Prevents rollover, minor limitation on prefix is that it must not end in \xff
            assert prefix[-1] != '\xff'
            prefix_start_row = prefix
            prefix_stop_row = prefix[:-1] + chr(ord(prefix[-1]) + 1)
            if start_row and prefix_start_row <= start_row < prefix_stop_row and stop_row and prefix_start_row <= stop_row <= prefix_stop_row:
                return
        bottle.abort(401)

    def _row_validate(self, row, permissions, thrift=None):
        prefixes = self.image_prefixes
        permissions = set(permissions)
        prefixes = [x for x, y in prefixes.items() if set(y).issuperset(permissions)]
        for prefix in prefixes:
            if prefix == '':
                return
            # NOTE: Prevents rollover, minor limitation on prefix is that it must not end in \xff
            assert prefix[-1] != '\xff'
            prefix_start_row = prefix
            prefix_stop_row = prefix[:-1] + chr(ord(prefix[-1]) + 1)
            if row and prefix_start_row <= row < prefix_stop_row:
                return
        bottle.abort(401)

    def _column_write_validate(self, column):
        if column == 'data:image':
            return
        if column.startswith('meta:'):
            return
        bottle.abort(403)

    def post_table(self, params, files):
        row = self.upload_row_prefix + '%.10d%s' % (2147483648 - int(time.time()), uuid.uuid4().bytes)
        self.patch_row(row, params, files)
        return {'row': base64.urlsafe_b64encode(row)}

    def get_row(self, row, columns):
        self._row_validate(row, 'r')
        with thrift_lock() as thrift:
            if columns:
                result = thrift.getRowWithColumns(self.table, row, columns)
            else:
                result = thrift.getRow(self.table, row)
        if not result:
            bottle.abort(404)
        # TODO: Should this produce 'row' also?  Check backbone.js
        return {base64.urlsafe_b64encode(x): base64.b64encode(y.value)
                for x, y in result[0].columns.items()}

    def post_row(self, row, params, files):
        action = params['action']
        with thrift_lock() as thrift:
            manager = PicarusManager(thrift=thrift)
            model_key = base64.urlsafe_b64decode(params['model'])
            # TODO: Allow io/ so that we can write back to the image too
            if action == 'i/link':
                self._row_validate(row, 'r')
                chain_input, model_link = _takeout_model_link_from_key(manager, model_key)
                binary_input = thrift.get(self.table, row, chain_input)[0].value  # TODO: Check val
                model = picarus_takeout.ModelLink(json.dumps(model_link))
                bottle.response.headers["Content-type"] = "application/json"
                return json.dumps({params['model']: base64.b64encode(model.process_binary(binary_input))})
            elif action == 'i/chain':
                self._row_validate(row, 'r')
                chain_inputs, model_chain = zip(*_takeout_model_chain_from_key(manager, model_key))
                binary_input = thrift.get(self.table, row, chain_inputs[0])[0].value  # TODO: Check val
                model = picarus_takeout.ModelChain(json.dumps(model_chain))
                bottle.response.headers["Content-type"] = "application/json"
                return json.dumps({params['model']: base64.b64encode(model.process_binary(binary_input))})
            else:
                bottle.abort(400)

    def _byte_count_rows(self, lod_rows, row_bytes=0, column_bytes=0):
        byte_count = 0
        for lod_row in lod_rows:
            byte_count += sum(len(x) + len(y) for x, y in lod_row.items())
            byte_count += row_bytes + column_bytes * len(lod_row)
        return byte_count

    def get_slice(self, start_row, stop_row, columns, params, files):
        self._slice_validate(start_row, stop_row, 'r')
        max_rows = min(10000, int(params.get('maxRows', 1)))
        max_bytes = min(1048576, int(params.get('maxBytes', 1048576)))
        filter_string = params.get('filter')
        print('filter string[%s]' % filter_string)
        exclude_start = bool(int(params.get('excludeStart', 0)))
        with thrift_lock() as thrift:
            scanner = hadoopy_hbase.scanner(thrift, self.table, per_call=10, columns=columns,
                                            start_row=start_row, stop_row=stop_row, filter=filter_string)
        out = []
        cur_row = start_row
        byte_count = 0
        for row_num, (cur_row, cur_columns) in enumerate(scanner, 1):
            if exclude_start and row_num == 1:
                continue
            out.append(encode_row(cur_row, cur_columns))
            byte_count += self._byte_count_rows(out[-1:])
            if len(out) >= max_rows or byte_count >= max_bytes:
                break
        bottle.response.headers["Content-type"] = "application/json"
        return json.dumps(out)

    def patch_slice(self, start_row, stop_row, params, files):
        self._slice_validate(start_row, stop_row, 'w')
        # NOTE: This only fetches rows that have a column in data:image (it is a significant optimization)
        # NOTE: Only parameters allowed, no "files" due to memory restrictions
        mutations = []
        for x, y in params.items():
            mutations.append(hadoopy_hbase.Mutation(column=base64.urlsafe_b64decode(x), value=base64.b64decode(y)))
        if mutations:
            with thrift_lock() as thrift:
                for row, _ in hadoopy_hbase.scanner(thrift, self.table, start_row=start_row, stop_row=stop_row, filter='KeyOnlyFilter()', columns=['data:image']):
                    thrift.mutateRow(self.table, row, mutations)
        return {}

    def post_slice(self, start_row, stop_row, params, files):
        action = params['action']
        with thrift_lock() as thrift:
            manager = PicarusManager(thrift=thrift)
            if action == 'io/thumbnail':
                self._slice_validate(start_row, stop_row, 'rw')
                manager.image_thumbnail(start_row=start_row, stop_row=stop_row)
                return {}
            elif action == 'io/exif':
                self._slice_validate(start_row, stop_row, 'rw')
                manager.image_exif(start_row=start_row, stop_row=stop_row)
                return {}
            elif action == 'io/preprocess':
                self._slice_validate(start_row, stop_row, 'rw')
                manager.image_preprocessor(base64.urlsafe_b64decode(params['model']), start_row=start_row, stop_row=stop_row)
                return {}
            elif action == 'io/classify':
                self._slice_validate(start_row, stop_row, 'rw')
                manager.feature_to_prediction(base64.urlsafe_b64decode(params['model']), start_row=start_row, stop_row=stop_row)
                return {}
            elif action == 'io/feature':
                self._slice_validate(start_row, stop_row, 'rw')
                manager.takeout_link_job(base64.urlsafe_b64decode(params['model']), start_row=start_row, stop_row=stop_row)
                return {}
            elif action == 'io/link':
                self._slice_validate(start_row, stop_row, 'rw')
                model_key = base64.urlsafe_b64decode(params['model'])
                chain_input, model_link = _takeout_model_link_from_key(manager, model_key)
                manager.takeout_link_job(model_link, chain_input, model_key, start_row=start_row, stop_row=stop_row)
                return {}
            elif action == 'io/chain':
                self._slice_validate(start_row, stop_row, 'rw')
                model_key = base64.urlsafe_b64decode(params['model'])
                chain_inputs, model_chain = zip(*_takeout_model_chain_from_key(manager, model_key))
                manager.takeout_chain_job(list(model_chain), chain_inputs[0], model_key, start_row=start_row, stop_row=stop_row)
                return {}
            elif action == 'io/hash':
                self._slice_validate(start_row, stop_row, 'rw')
                manager.feature_to_hash(base64.urlsafe_b64decode(params['model']), start_row=start_row, stop_row=stop_row)
                return {}
            elif action == 'i/dedupe/identical':
                self._slice_validate(start_row, stop_row, 'r')
                col = base64.urlsafe_b64decode(params['column'])
                features = {}
                dedupe_feature = lambda x, y: features.setdefault(base64.b64encode(hashlib.md5(y).digest()), []).append(base64.urlsafe_b64encode(x))
                for cur_row, cur_col in hadoopy_hbase.scanner_row_column(thrift, self.table, column=col,
                                                                         start_row=start_row, per_call=10,
                                                                         stop_row=stop_row):
                    dedupe_feature(cur_row, cur_col)
                bottle.response.headers["Content-type"] = "application/json"
                return json.dumps([{'rows': y} for x, y in features.items() if len(y) > 1])
            elif action == 'o/crawl/flickr':
                self._slice_validate(start_row, stop_row, 'w')
                # Only slices where the start_row can be used as a prefix may be used
                assert start_row and ord(start_row[-1]) != 255 and start_row[:-1] + chr(ord(start_row[-1]) + 1) == stop_row
                p = {}
                row_prefix = start_row
                assert row_prefix.find(':') != -1
                class_name = params['className']
                query = params.get('query')
                query = class_name if query is None else query
                p['lat'] = query = params.get('lat')
                p['lon'] = query = params.get('lon')
                p['radius'] = query = params.get('radius')
                p['api_key'] = params.get('apiKey', FLICKR_API_KEY)
                p['api_secret'] = params.get('apiSecret', FLICKR_API_SECRET)
                if 'hasGeo' in params:
                    p['has_geo'] = params['hasGeo'] == '1'
                try:
                    p['min_upload_date'] = int(params['minUploadDate'])
                except KeyError:
                    pass
                try:
                    p['max_upload_date'] = int(params['maxUploadDate'])
                except KeyError:
                    pass
                try:
                    p['page'] = int(params['page'])
                except KeyError:
                    pass
                return {'numRows': crawlers.flickr_crawl(crawlers.HBaseCrawlerStore(thrift, row_prefix), class_name, query, **p)}
            elif action in ('io/annotate/image/query', 'io/annotate/image/entity', 'io/annotate/image/query_batch'):
                self._slice_validate(start_row, stop_row, 'r')
                secret = base64.urlsafe_b64encode(uuid.uuid4().bytes)[:-2]
                task = base64.urlsafe_b64encode(uuid.uuid4().bytes)[:-2]
                p = {}
                image_column = base64.urlsafe_b64decode(params['imageColumn'])
                if action == 'io/annotate/image/entity':
                    entity_column = base64.urlsafe_b64decode(params['entityColumn'])
                    data = 'hbase://localhost:9090/images/%s/%s?entity=%s&image=%s' % (base64.urlsafe_b64encode(start_row), base64.urlsafe_b64encode(stop_row),
                                                                                       entity_column, image_column)
                    p['type'] = 'image_entity'
                elif action == 'io/annotate/image/query':
                    query = params['query']
                    data = 'hbase://localhost:9090/images/%s/%s?image=%s' % (base64.urlsafe_b64encode(start_row), base64.urlsafe_b64encode(stop_row), image_column)
                    p['type'] = 'image_query'
                    p['query'] = query
                elif action == 'io/annotate/image/query_batch':
                    query = params['query']
                    data = 'hbase://localhost:9090/images/%s/%s?image=%s' % (base64.urlsafe_b64encode(start_row), base64.urlsafe_b64encode(stop_row), image_column)
                    p['type'] = 'image_query_batch'
                    p['query'] = query
                else:
                    bottle.abort(400)
                p['num_tasks'] = 100
                p['mode'] = 'standalone'
                try:
                    redis_host, redis_port = ANNOTATORS.add_task(task, self.owner, secret, data, p).split(':')
                except annotators.CapacityException:
                    bottle.abort(503)
                p['setup'] = True
                p['reset'] = True
                p['secret'] = secret
                p['redis_address'] = redis_host
                p['redis_port'] = int(redis_port)
                mturk_vision.manager(data=data, **p)
                return {'task': task}
            else:
                bottle.abort(400)


def parse_slices():
    if bottle.request.content_type == "application/json":
        return bottle.request.json['slices']
    else:
        if not bottle.request.params['slices']:
            return []
        return bottle.request.params['slices'].split(',')


class ModelsHBaseTable(HBaseTable):

    def __init__(self, _auth_user):
        super(ModelsHBaseTable, self).__init__(_auth_user, 'picarus_models')
        self._auth_user = _auth_user

    def _column_write_validate(self, column):
        if column in ('data:notes', 'data:tags'):
            return
        if column.startswith('user:'):
            return
        bottle.abort(403)

    def _row_validate(self, row, permissions, thrift):
        results = thrift.get('picarus_models', row, 'user:' + self.owner)
        if not results:
            bottle.abort(403)
        if not results[0].value.startswith(permissions):
            bottle.abort(403)

    def get_table(self, columns):
        user_column = 'user:' + self.owner
        output_user = user_column in columns or not columns or 'user:' in columns
        hbase_filter = "SingleColumnValueFilter ('user', '%s', =, 'binaryprefix:r', true, true)" % self.owner
        outs = []
        with thrift_lock() as thrift:
            for row, cols in hadoopy_hbase.scanner(thrift, self.table, columns=columns + [user_column], filter=hbase_filter):
                self._row_validate(row, 'r', thrift)
                if not output_user:
                    del cols[user_column]
                outs.append(encode_row(row, cols))
        bottle.response.headers["Content-type"] = "application/json"
        return json.dumps(outs)

    def post_row(self, row, params, files):
        action = params['action']
        with thrift_lock() as thrift:
            manager = PicarusManager(thrift=thrift)
            if action == 'i/takeout/link':
                self._row_validate(row, 'r', thrift)
                bottle.response.headers["Content-type"] = "application/json"
                return json.dumps(_takeout_model_link_from_key(manager, row)[1], separators=(',', ':'))
            elif action == 'i/takeout/chain':
                self._row_validate(row, 'r', thrift)
                bottle.response.headers["Content-type"] = "application/json"
                return json.dumps(zip(*_takeout_model_chain_from_key(manager, row))[1], separators=(',', ':'))
            else:
                bottle.abort(400)

    def post_table(self, params, files):
        path = params['path']
        with thrift_lock() as thrift:
            manager = PicarusManager(thrift=thrift)
            if path.startswith('model/'):
                return _create_model_from_params(manager, self.owner, path, params)
            elif path.startswith('factory/'):
                table = params['table']
                slices = parse_slices()
                start_stop_rows = [map(base64.urlsafe_b64decode, s.split('/')) for s in slices]
                data_table = get_table(self._auth_user, table)
                for start_row, stop_row in start_stop_rows:
                    data_table._slice_validate(start_row, stop_row, 'r')

                def classifier_sklearn(params, inputs, schema):
                    label_features = {0: [], 1: []}
                    for start_row, stop_row in start_stop_rows:
                        row_cols = hadoopy_hbase.scanner(thrift, data_table.table,
                                                         columns=[base64.urlsafe_b64decode(inputs['feature']), base64.urlsafe_b64decode(inputs['meta'])],
                                                         start_row=start_row, stop_row=stop_row)
                        for row, cols in row_cols:
                            try:
                                label = int(cols[base64.urlsafe_b64decode(inputs['meta'])] == params['class_positive'])
                                label_features[label].append(cols[base64.urlsafe_b64decode(inputs['feature'])])
                            except KeyError:
                                continue
                    labels = [0] * len(label_features[0]) + [1] * len(label_features[1])
                    features = label_features[0] + label_features[1]
                    features = np.asfarray([msgpack.unpackb(x)[0] for x in features])
                    import sklearn.svm
                    classifier = sklearn.svm.LinearSVC()
                    classifier.fit(features, np.asarray(labels))
                    factory_info = {'slices': slices, 'num_rows': len(features), 'data': 'slices', 'params': params, 'inputs': inputs}
                    model = {'name': 'picarus.LinearClassifier', 'kw': {'coefficients': map(PrettyFloat, classifier.coef_.tolist()[0]),
                                                                        'intercept': PrettyFloat(classifier.intercept_[0])}}
                    return {'input': inputs['feature'], 'model': model, 'input_type': 'feature', 'output_type': 'binary_class_confidence',
                            'email': self.owner, 'name': manager.model_to_name(model), 'factory_info': json.dumps(factory_info)}

                def classifier_class_distance_list(model_dict, model_param, inputs):
                    row_cols = hadoopy_hbase.scanner(thrift, self.table,
                                                     columns=[inputs['multi_feature'], inputs['meta']], start_row=start_row, stop_row=stop_row)

                    #label_values = ((cols[inputs['meta']], np.asfarray(picarus.api.np_fromstring(cols[inputs['multi_feature']]))) for _, cols in row_cols)
                    def gen():
                        for _, cols in row_cols:
                            yield cols[inputs['meta']], np.asfarray(picarus.api.np_fromstring(cols[inputs['multi_feature']]))
                    classifier = call_import(model_dict)
                    classifier.train(gen())  # label_values
                    return classifier

                def hasher_train(model_dict, model_param, inputs):
                    hasher = call_import(model_dict)
                    features = hadoopy_hbase.scanner_column(thrift, self.table, inputs['feature'],
                                                            start_row=start_row, stop_row=stop_row)
                    return hasher.train(picarus.api.np_fromstring(x) for x in features)

                def index_train(model_dict, model_param, inputs):
                    index = call_import(model_dict)
                    row_cols = hadoopy_hbase.scanner(thrift, self.table,
                                                     columns=[inputs['hash'], inputs['meta']], start_row=start_row, stop_row=stop_row)
                    metadata, hashes = zip(*[(json.dumps([cols[inputs['meta']], base64.urlsafe_b64encode(row)]), cols[inputs['hash']])
                                             for row, cols in row_cols])
                    hashes = np.ascontiguousarray(np.asfarray([np.fromstring(h, dtype=np.uint8) for h in hashes]))
                    index = index.store_hashes(hashes, np.arange(len(metadata), dtype=np.uint64))
                    index.metadata = metadata
                    return index

                def kmeans_cluster_mfeat(model_dict, model_param, inputs):
                    # TODO: This needs to be finished, determine if we want quantizer level or cluster level
                    clusterer = call_import(model_dict)
                    features = []
                    row_cols = hadoopy_hbase.scanner(thrift, self.table,
                                                     columns=[inputs['multi_feature']], start_row=start_row, stop_row=stop_row)
                    # TODO: We'll want to check that we aren't clustering too much data by placing constraints
                    for row, columns in row_cols:
                        features.append(picarus.api.np_fromstring(columns[inputs['multi_feature']]))
                    features = np.vstack(features)
                    return clusterer.cluster(features)

                if path == 'factory/classifier/svmlinear':
                    return _create_model_from_factory(manager, self.owner, path, classifier_sklearn, params)
                #elif path == 'classifier/nbnnlocal':
                #    return _create_model_from_factory(manager, self.owner, path, classifier_class_distance_list, params, start_row=start_row, stop_row=stop_row)
                #elif path == 'hasher/rrmedian':
                #    return _create_model_from_factory(manager, self.owner, path, hasher_train, params, start_row=start_row, stop_row=stop_row)
                #elif path == 'index/linear':
                #    return _create_model_from_factory(manager, self.owner, path, index_train, params, start_row=start_row, stop_row=stop_row)
                else:
                    bottle.abort(400)


def get_table(_auth_user, table):
    annotation_re = re.search('annotations\-(results|users)\-([a-zA-Z0-9_\-]+)', table)
    if annotation_re:
        return AnnotationDataTable(_auth_user, *annotation_re.groups())
    elif table == 'annotations':
        return AnnotationsTable(_auth_user)
    elif table == 'images':
        return ImagesHBaseTable(_auth_user)
    elif table == 'models':
        return ModelsHBaseTable(_auth_user)
    elif table == 'models':
        return ModelsHBaseTable(_auth_user)
    elif table == 'prefixes':
        return PrefixesTable(_auth_user)
    elif table == 'parameters':
        return ParametersTable()
    elif table == 'users':
        return UsersTable(_auth_user)
    else:
        bottle.abort(404)