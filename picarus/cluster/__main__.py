import hadoopy
import os
import tempfile
import numpy as np
import cPickle as pickle


def run_whiten(hdfs_input, hdfs_output, **kw):
    hadoopy.launch_frozen(hdfs_input, hdfs_output, 'whiten.py')


def run_sample(hdfs_input, hdfs_output, num_clusters, **kw):
    hadoopy.launch_frozen(hdfs_input, hdfs_output, 'random_sample.py',
                          cmdenvs=['SAMPLE_SIZE=%d' % num_clusters])


def run_kmeans(hdfs_input, hdfs_prev_clusters, hdfs_image_data, hdfs_output, num_clusters,
               num_iters, num_samples, metric, local_json_output=None, **kw):
    frozen_tar_path = None
    for cur_iter_num in range(num_iters):
        clusters_fp = fetch_clusters_from_hdfs(hdfs_prev_clusters)
        clusters_fn = os.path.basename(clusters_fp.name)
        cur_output = '%s/clust%.6d' % (hdfs_output, cur_iter_num)
        frozen_tar_path = hadoopy.launch_frozen(hdfs_input, cur_output, 'kmeans.py',
                                                cmdenvs=['CLUSTERS_FN=%s' % clusters_fn],
                                                files=[clusters_fp.name],
                                                num_reducers=max(1, num_clusters / 2),
                                                frozen_tar_path=frozen_tar_path,
                                                dummy_arg=clusters_fp)['frozen_tar_path']
        hdfs_prev_clusters = cur_output
    print('Clusters[%s]' % hdfs_prev_clusters)
    # Compute K-Means assignment/samples
    # TODO Do full assignment, then sample
    clusters_fp = fetch_clusters_from_hdfs(hdfs_prev_clusters)
    clusters_fn = os.path.basename(clusters_fp.name)
    cur_output = '%s/assign' % hdfs_output
    hadoopy.launch_frozen(hdfs_input, cur_output, 'kmeans_assign.py',
                          cmdenvs=['CLUSTERS_FN=%s' % clusters_fn,
                                   'NUM_SAMPLES=%d' % num_samples,
                                   'mapred.text.key.partitioner.options=-k1'],
                          files=[clusters_fp.name],
                          num_reducers=max(1, num_clusters / 2),
                          partitioner='org.apache.hadoop.mapred.lib.KeyFieldBasedPartitioner',
                          dummy_arg=clusters_fp)
    print('Assignment[%s]' % cur_output)
    # Filter the samples
    assignments_fp = fetch_assignments_from_hdfs(cur_output)
    assignments_fn = os.path.basename(assignments_fp.name)
    cur_output = '%s/samples' % hdfs_output
    hadoopy.launch_frozen(hdfs_image_data, cur_output, 'filter_samples.py',
                          cmdenvs=['ASSIGNMENTS_FN=%s' % os.path.basename(assignments_fn)],
                          files=[assignments_fp.name],
                          reducer=None,
                          dummy_arg=assignments_fp)
    print('Samples[%s]' % cur_output)


def run_hac(**kw):
    pass


def fetch_clusters_from_hdfs(hdfs_input):
    """Fetch remote clusters and store locally

    Clusters are sorted to allow comparing between iterations

    Args:
        hdfs_input: HDFS input path

    Returns:
        NamedTemporaryFile holding the cluster data
    """
    clusters_fp = tempfile.NamedTemporaryFile()
    clusters = [v.tolist() for k, v in hadoopy.readtb(hdfs_input)]
    clusters.sort()
    clusters = np.ascontiguousarray(clusters, dtype=np.float64)
    pickle.dump(clusters, clusters_fp, -1)
    clusters_fp.seek(0)
    return clusters_fp


def fetch_assignments_from_hdfs(hdfs_input):
    """Fetch remote assignments and store locally

    Args:
        hdfs_input: HDFS input path

    Returns:
        NamedTemporaryFile holding the assignment data
    """
    assignments_fp = tempfile.NamedTemporaryFile()
    assignments = list(hadoopy.readtb(hdfs_input))
    pickle.dump(assignments, assignments_fp, -1)
    assignments_fp.seek(0)
    return assignments_fp
