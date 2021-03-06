import random
import time
import zlib
import tempfile
import msgpack
import pickle
import redis
from PIL.ExifTags import TAGS
from PIL import Image
import cStringIO as StringIO
import json
import picarus_takeout
import bottle
import base64
import hadoopy_hbase
import os
import re
import crawlers
import logging
import driver
import tables
import hashlib
try:
    from flickr_keys import FLICKR_API_KEY, FLICKR_API_SECRET
except ImportError:
    logging.warn('No default flickr keys found in flickr_keys.py, see flickr_keys.example.py')
    FLICKR_API_KEY, FLICKR_API_SECRET = '', ''


def _tempfile(data, suffix=''):
    fp = tempfile.NamedTemporaryFile(suffix=suffix)
    fp.write(data)
    fp.flush()
    return fp


def model_tofile(model):
    if isinstance(model, dict) or isinstance(model, list):
        return _tempfile(zlib.compress(msgpack.dumps(model)), suffix='.msgpack.gz')
    else:
        return _tempfile(zlib.compress(pickle.dumps(model)), suffix='.pkl.gz')


def hadoop_wait_till_started(launch_out):
    process = launch_out['process']
    stdout = process.stderr
    while process.poll() is None:
        line = stdout.readline()
        print(line)
        if line.find('Tracking URL:') != -1:
            break
    if process.poll() > 0:
        raise RuntimeError('Hadoop task could not start')


def async(func):

    def inner(self, *args, **kw):
        if self._local:
            return func(self, *args, **kw)
        self._jobs.add_work(True, 'default', func=func.__name__, method_args=args, method_kwargs=kw)
    return inner


def factory(database, local, jobs, **kw):
    if database == 'redis':
        return RedisDB(kw['redis_host'], kw['redis_port'], 2, jobs, local)
    if database == 'hbase':
        return HBaseDB(kw['thrift_server'], kw['thrift_port'], jobs, local)
    if database == 'hbasehadoop':
        return HBaseDBHadoop(kw['thrift_server'], kw['thrift_port'], jobs, local)
    raise ValueError('Unknown option[%s]' % database)


class BaseDB(object):

    def __init__(self, jobs, local=False):
        if hasattr(self, 'args'):
            self.args += [jobs, True]
        else:
            self.args = [jobs, True]
        self._jobs = jobs
        self._local = local
        super(BaseDB, self).__init__()

    def __reduce__(self):
        return (BaseDB, tuple(self.args))

    def _row_job(self, table, start_row, stop_row, input_column, output_column, func, job_row):
        good_rows, total_rows = 0, 0
        for row, columns in self.scanner(table, start_row, stop_row, columns=[input_column]):
            total_rows += 1
            try:
                input_data = columns[input_column]
            except KeyError:
                continue
            try:
                output_data = func(input_data)
            except:
                # TODO: We need some way of reporting exceptions back
                continue
            if output_data is None:
                continue
            self.mutate_row(table, row, {output_column: output_data})
            good_rows += 1
            self._jobs.update_task(job_row, {'goodRows': good_rows, 'badRows': total_rows - good_rows, 'status': 'running'})
        self._jobs.update_task(job_row, {'goodRows': good_rows, 'badRows': total_rows - good_rows, 'status': 'completed'})

    @async
    def exif_job(self, start_row, stop_row, job_row):

        def func(input_data):
            image = Image.open(StringIO.StringIO(input_data))
            if not hasattr(image, '_getexif'):
                return json.dumps({})
            else:
                image_tags = image._getexif()
                if image_tags is None:
                    return json.dumps({})
                else:
                    return json.dumps(dict((name, base64.b64encode(image_tags[id]) if isinstance(image_tags[id], str) else image_tags[id])
                                           for id, name in TAGS.items()
                                           if id in image_tags))
        self._row_job('images', start_row, stop_row, 'data:image', 'meta:exif', func, job_row)

    @async
    def copy_job(self, table, input_column, output_column, start_row, stop_row, job_row):
        self._row_job('images', start_row, stop_row, input_column, output_column, lambda x: x, job_row)

    @async
    def takeout_chain_job(self, table, model, input_column, output_column, start_row, stop_row, job_row):
        model = picarus_takeout.ModelChain(msgpack.dumps(model))

        def func(input_data):
            return model.process_binary(input_data)
        self._row_job(table, start_row, stop_row, input_column, output_column, func, job_row)

    @async
    def street_view_job(self, params, start_row, stop_row, job_row):
        # Only slices where the start_row can be used as a prefix may be used
        assert start_row and ord(start_row[-1]) != 255 and start_row[:-1] + chr(ord(start_row[-1]) + 1) == stop_row
        p = {}
        row_prefix = start_row
        try:
            p['api_key'] = params['apiKey']
            p['lat'] = float(params['lat'])
            p['lon'] = float(params['lon'])
            if 'locationDelta' in params:
                p['incr'] = float(params['locationDelta'])
            if 'gridRadius' in params:
                p['grid_radius'] = int(params['gridRadius'])
                if p['grid_radius'] < 0:
                    raise ValueError
            if 'headingDelta' in params:
                p['heading_delta'] = float(params['headingDelta'])
            if 'pitch' in params:
                p['pitch'] = float(params['pitch'])
            if 'fov' in params:
                p['fov'] = float(params['fov'])
        except (KeyError, ValueError):
            bottle.abort(400, 'Invalid crawler parameters')

        job_columns = {'goodRows': 0, 'badRows': 0, 'status': 'running'}
        row_latlon = {}  # [row] = [[lat, lon]]

        def store(crawl_kwargs, image, source, **kw):
            cols = {}
            md5 = lambda x: hashlib.md5(x).digest()
            cur_md5 = md5(image)
            cols['data:image'] = image
            cols['meta:source'] = source
            cols['hash:md5'] = cur_md5
            for x, y in kw.items():
                cols['meta:' + x] = y
            row = row_prefix + cur_md5
            try:
                row_latlon.setdefault(row, []).append([kw['latitude'], kw['longitude']])
            except KeyError:
                pass
            self.mutate_row('images', row, cols)
            job_columns['goodRows'] += 1
            self._jobs.update_task(job_row, job_columns)
        crawlers.street_view_crawl(store, **p)
        for row, latlons in row_latlon.items():
            self.mutate_row('images', row, {'meta:latlons': json.dumps(latlons)})
        job_columns['status'] = 'completed'
        self._jobs.update_task(job_row, job_columns)

    @async
    def flickr_job(self, params, start_row, stop_row, job_row):
        # Only slices where the start_row can be used as a prefix may be used
        assert start_row and ord(start_row[-1]) != 255 and start_row[:-1] + chr(ord(start_row[-1]) + 1) == stop_row
        p = {}
        row_prefix = start_row
        p['class_name'] = params.get('className')
        p['query'] = params.get('query')
        p['lat'] = params.get('lat')
        p['lon'] = params.get('lon')
        if not ((p['lat'] and p['lon']) or p['query']):
            bottle.abort(400, 'Invalid crawler parameters')
        p['radius'] = params.get('radius')
        p['size'] = params.get('size', 'm')
        if p['size'] not in ('sq', 't', 's', 'q', 'm', 'n', 'z', 'c', 'l', 'o'):
            bottle.abort(400, 'Invalid size')
        p['api_key'] = params.get('apiKey', FLICKR_API_KEY)
        p['api_secret'] = params.get('apiSecret', FLICKR_API_SECRET)
        if not p['api_key'] or not p['api_secret']:
            bottle.abort(400)  # Either we don't have a default or the user provided an empty key
        if 'hasGeo' in params:
            p['has_geo'] = params['hasGeo'] == '1'
        if 'onePerOwner' in params:
            p['one_per_owner'] = params['onePerOwner'] == '1'
        try:
            p['max_rows'] = int(params['maxRows'])
        except KeyError:
            pass
        iterations = min(10000, int(params.get('iterations', 1)))
        try:
            min_upload_date = int(params['minUploadDate'])
        except KeyError:
            min_upload_date = None
        try:
            max_upload_date = int(params['maxUploadDate'])
        except KeyError:
            max_upload_date = None
        try:
            upload_date_radius = int(params['uploadDateRadius'])
            if max_upload_date is None:
                max_upload_date = int(time.time())
            if min_upload_date is None:
                min_upload_date = 1232170610
        except KeyError:
            upload_date_radius = None
            if max_upload_date is not None:
                p['max_upload_date'] = max_upload_date
            if min_upload_date is not None:
                p['min_upload_date'] = min_upload_date
        if iterations > 1 and upload_date_radius is None:
            bottle.abort(400)  # Need to specify a radius for multiple iters or else we will return same vals
        try:
            p['page'] = int(params['page'])
        except KeyError:
            pass
        job_columns = {'goodRows': 0, 'badRows': 0, 'status': 'running'}

        def store(crawl_kwargs, image, source, **kw):
            print('In store')
            # TODO: Need to extend onePerOwner to multiple iterations
            query = crawl_kwargs.get('query')
            class_name = crawl_kwargs.get('class_name')
            cols = {}
            md5 = lambda x: hashlib.md5(x).digest()
            cur_md5 = md5(image)
            cols['data:image'] = image
            if class_name is not None:
                cols['meta:class'] = class_name
            if query is not None:
                cols['meta:query'] = query
            cols['meta:source'] = source
            cols['hash:md5'] = cur_md5
            for x, y in kw.items():
                cols['meta:' + x] = y
            row = row_prefix + cur_md5
            print('Pre mutate row')
            self.mutate_row('images', row, cols)
            print('Post mutate row')
            job_columns['goodRows'] += 1
            self._jobs.update_task(job_row, job_columns)

        for n in range(iterations):
            print('Iter[%d]' % n)
            if upload_date_radius:
                p['min_upload_date'] = random.randint(min_upload_date, max_upload_date - upload_date_radius)
                p['max_upload_date'] = p['min_upload_date'] + upload_date_radius
            crawlers.flickr_crawl(store, **p)
        job_columns['status'] = 'completed'
        self._jobs.update_task(job_row, job_columns)

    @async
    def create_model_job(self, create_model, params, inputs, schema, start_stop_rows, table, email, job_row):
        # Give the model creator an iterator of row, cols (where cols are the input names)
        job_columns = {'goodRows': 0, 'badRows': 0, 'status': 'running'}
        os.nice(5)  # These are background tasks, don't let the CPU get too crazy

        def inner():
            total_rows = 0
            for start_row, stop_row in start_stop_rows:
                row_cols = self.scanner(table, columns=inputs.values(), start_row=start_row, stop_row=stop_row)
                for row, columns in row_cols:
                    total_rows += 1
                    try:
                        yield row, dict((pretty_column, columns[raw_column]) for pretty_column, raw_column in inputs.items())
                        job_columns['goodRows'] += 1
                        job_columns['badRows'] = total_rows - job_columns['goodRows']
                        self._jobs.update_task(job_row, job_columns)
                    except KeyError:
                        continue
        input_type, output_type, model_link = create_model(inner(), params)
        slices = [base64.b64encode(start_row) + ',' + base64.b64encode(stop_row) for start_row, stop_row in start_stop_rows]
        inputsb64 = dict((k, base64.b64encode(v)) for k, v in inputs.items())
        factory_info = {'slices': slices, 'num_rows': job_columns['goodRows'], 'data': 'slices', 'params': params, 'inputs': inputsb64}
        manager = driver.PicarusManager(db=self)
        model_chain = tables._takeout_model_chain_from_key(manager, inputs[input_type]) + [model_link]
        job_columns['modelRow'] = manager.input_model_param_to_key(**{'input': inputs[input_type], 'model_link': model_link, 'model_chain': model_chain, 'input_type': input_type,
                                                                      'output_type': output_type, 'email': email, 'name': manager.model_to_name(model_link),
                                                                      'factory_info': json.dumps(factory_info)})
        job_columns['status'] = 'completed'
        self._jobs.update_task(job_row, job_columns)


class RedisDB(BaseDB):

    def __init__(self, server, port, db, *args, **kw):
        if hasattr(self, 'args'):
            self.args += [server, port, db]
        else:
            self.args = [server, port, db]
        self.__redis = redis.StrictRedis(host=server, port=port, db=db)
        # redis[table:row] -> data[table][row]
        super(RedisDB, self).__init__(*args, **kw)

    def __reduce__(self):
        return (RedisDB, tuple(self.args))

    def _get_columns(self, table, row, columns, keys_only=False):
        table_row = table + ':' + row
        keys = self.__redis.hkeys(table_row)
        cfs = set()
        cols = set()
        for c in columns:
            cur_cf, cur_col = c.split(':', 1)
            if cur_col:
                cols.add(c)
            else:
                cfs.add(cur_cf)
        out = {}
        for k in keys:
            if k in cols or k.split(':', 1)[0] in cfs:
                if keys_only:
                    out[k] = ''
                else:
                    d = self.__redis.hget(table_row, k)
                    if d is not None:
                        out[k] = d
        return out

    def mutate_row(self, table, row, mutations):
        self.__redis.hmset(table + ':' + row, mutations)

    def delete_row(self, table, row):
        self.__redis.delete(table + ':' + row)

    def delete_column(self, table, row, column):
        self.__redis.hdel(table + ':' + row, column)

    def get_row(self, table, row, columns=None, check=True, keys_only=False):
        if columns:
            result = self._get_columns(table, row, columns, keys_only=keys_only)
        else:
            if keys_only:
                result = dict((x, '') for x in self.__redis.hkeys(table + ':' + row))
            else:
                result = self.__redis.hgetall(table + ':' + row)
        if check and not result:
            bottle.abort(404)
        return result

    def get_column(self, table, row, column):
        out = self.__redis.hget(table + ':' + row, column)
        if out is None:
            bottle.abort(404)
        return out

    def scanner(self, table, start_row=None, stop_row=None, columns=None, keys_only=False, per_call=1, column_filter=None):
        keep_row = lambda x: True
        if column_filter:
            filter_column = column_filter[0]
            if column_filter[1] == '=':
                keep_row = lambda x: filter_column in x and x[filter_column] == column_filter[2]
            elif column_filter[1] == '!=':
                keep_row = lambda x: filter_column in x and x[filter_column] != column_filter[2]
            elif column_filter[1] == 'startswith':
                keep_row = lambda x: filter_column in x and x[filter_column].startswith(column_filter[2])
            else:
                bottle.abort(400)  # Bad filter
        if start_row is not None and stop_row is not None:
            prefix = []
            for x, y in zip(start_row, stop_row):
                if x == y:
                    prefix.append(x)
                else:
                    break
            prefix = ''.join(prefix)
        else:
            prefix = ''
        table_start_row = table + ':' if start_row is None else '%s:%s' % (table, start_row)
        table_stop_row = None if stop_row is None else '%s:%s' % (table, stop_row)
        for row in self.__redis.keys('%s:%s*' % (table, prefix)):
            if row < table_start_row or (table_stop_row is not None and row >= table_stop_row):
                continue
            clean_row = row.split(':')[1]
            cur_row = self.get_row(table, clean_row, check=False, keys_only=keys_only)
            if not keep_row(cur_row):
                continue
            yield clean_row, cur_row


class HBaseDB(BaseDB):

    def __init__(self, server, port, *args, **kw):
        if hasattr(self, 'args'):
            self.args += [server, port]
        else:
            self.args = [server, port]
        self._thrift = hadoopy_hbase.connect(server, port, timeout=300000)
        self.num_mappers = 6
        super(HBaseDB, self).__init__(*args, **kw)

    def __reduce__(self):
        return HBaseDB, tuple(self.args)

    def mutate_row(self, table, row, mutations):
        mutations = [hadoopy_hbase.Mutation(column=x, value=y) for x, y in mutations.items()]
        self._thrift.mutateRow(table, row, mutations)

    def delete_row(self, table, row):
        self._thrift.deleteAllRow(table, row)

    def delete_column(self, table, row, column):
        self._thrift.mutateRow(table, row, [hadoopy_hbase.Mutation(column=column, isDelete=True)])

    def get_row(self, table, row, columns=None):
        if columns:
            result = self._thrift.getRowWithColumns(table, row, columns)
        else:
            result = self._thrift.getRow(table, row)
        if not result:
            bottle.abort(404)
        return dict((x, y.value) for x, y in result[0].columns.items())

    def get_column(self, table, row, column):
        try:
            return self._thrift.get(table, row, column)[0].value
        except IndexError:
            bottle.abort(404)

    def scanner(self, table, start_row=None, stop_row=None, columns=None, keys_only=False, per_call=1, column_filter=None):
        filts = ['KeyOnlyFilter()'] if keys_only else []
        if column_filter:
            sanitary = lambda x: re.search("^[a-zA-Z0-9@\.:]+$", x)
            filter_family, filter_column = column_filter[0].split(':')
            if column_filter[1] == '=':
                filter_relation = '='
                filter_value = 'binary:' + column_filter[2]
            elif column_filter[1] == '!=':
                filter_relation = '!='
                filter_value = 'binary:' + column_filter[2]
            elif column_filter[1] == 'startswith':
                filter_relation = '='
                filter_value = 'binaryprefix:' + column_filter[2]
            else:
                bottle.abort(400)  # Bad filter
            if any(not sanitary(x) for x in [filter_family, filter_column, filter_value]):
                bottle.abort(400)
            filts.append("SingleColumnValueFilter ('%s', '%s', %s, '%s', true, true)" % (filter_family, filter_column, filter_relation, filter_value))
        filt = ' AND '.join(filts)
        if not filt:
            filt = None
        return hadoopy_hbase.scanner(self._thrift, table, columns=columns,
                                     start_row=start_row, stop_row=stop_row, filter=filt, per_call=per_call)


class HBaseDBHadoop(HBaseDB):

    def __init__(self, *args, **kw):
        super(HBaseDBHadoop, self).__init__(*args, **kw)

    def __reduce__(self):
        return HBaseDBHadoop, tuple(self.args)

    @async
    def exif_job(self, start_row, stop_row, job_row):
        cmdenvs = {'HBASE_TABLE': 'images',
                   'HBASE_OUTPUT_COLUMN': base64.b64encode('meta:exif')}
        output_hdfs = 'picarus_temp/%f/' % time.time()
        hadoop_wait_till_started(hadoopy_hbase.launch('images', output_hdfs + str(random.random()), 'hadoop/image_exif.py', libjars=['hadoopy_hbase.jar'],
                                                      num_mappers=self.num_mappers, columns=['data:image'], single_value=True,
                                                      jobconfs={'mapred.task.timeout': '6000000', 'picarus.job.row': job_row}, cmdenvs=cmdenvs, check_script=False,
                                                      make_executable=False, start_row=start_row, stop_row=stop_row, name=job_row, wait=False))

    @async
    def takeout_chain_job(self, table, model, input_column, output_column, start_row, stop_row, job_row):
        output_hdfs = 'picarus_temp/%f/' % time.time()
        model_fp = model_tofile(model)
        cmdenvs = {'HBASE_TABLE': table,
                   'HBASE_OUTPUT_COLUMN': base64.b64encode(output_column),
                   'MODEL_FN': os.path.basename(model_fp.name)}
        hadoop_wait_till_started(hadoopy_hbase.launch(table, output_hdfs + str(random.random()), 'hadoop/takeout_chain_job.py', libjars=['hadoopy_hbase.jar'],
                                                      num_mappers=self.num_mappers, files=[model_fp.name], columns=[input_column], single_value=True,
                                                      jobconfs={'mapred.task.timeout': '6000000', 'picarus.job.row': job_row}, cmdenvs=cmdenvs, dummy_fp=model_fp,
                                                      check_script=False, make_executable=False,
                                                      start_row=start_row, stop_row=stop_row, name=job_row, wait=False))
