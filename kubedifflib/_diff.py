from functools import partial
import json
import os
import subprocess
import yaml


def iter_files(paths):
  """Yield absolute paths to all the files in 'paths'.

  'paths' is expected to be an iterable of paths to files or directories.
  Paths to files are yielded as is, paths to directories are recursed into.

  Equivalent to ``find "$paths[@]" -type f``.
  """
  # XXX: Copied from service/monitoring/lint
  for path in paths:
    if os.path.isfile(path):
      yield path
    else:
      for root, _dirs, filenames in os.walk(path):
        for filename in filenames:
          yield os.path.join(root, filename)


class KubeObject(object):
  """A Kubernetes object."""

  def __init__(self, namespace, kind, name):
    self.namespace = namespace
    self.kind = kind
    self.name = name

  @classmethod
  def from_dict(cls, data):
    """Construct a 'KubeObject' from a dictionary of Kubernetes data.

    'data' might be obtained from a Kubernetes cluster, or decoded from a YAML
    config file.
    """
    kind = data["kind"]
    name = data["metadata"]["name"]
    namespace = data["metadata"].get("namespace", "default")
    return cls(namespace, kind, name)

  def get_from_cluster(self, kubeconfig=None):
    """Fetch data for this object from a Kubernetes cluster.

    :param str kubeconfig: Path to a Kubernetes configuration file. If None,
        fetches data from the default cluster.
    :return: A dict of data for this Kubernetes object.
    """
    args = ["--namespace=%s" % self.namespace, "-o=yaml"]
    if kubeconfig is not None:
      args.append("--kubeconfig=%s" % kubeconfig)

    running = subprocess.check_output(["kubectl", "get"] + args + [self.kind, self.name], stderr=subprocess.STDOUT)
    return yaml.load(running)


class Difference(object):
  """An observed difference."""

  def __init__(self, message, path, *args):
    self.message = message
    self.path = path
    self.args = args

  def to_text(self):
    message = self.message % self.args
    if self.path is None:
      return message
    return '%s: %s' % (self.path, message)


def different_lengths(path, want, have):
  return Difference("Unequal lengths: %d != %d", path, len(want), len(have))

missing_item = partial(Difference, "'%s' missing")
not_equal = partial(Difference, "'%s' != '%s'")
broken_cluster = partial(Difference, " *** %s", None)


def diff_lists(path, want, have):
  if not len(want) == len(have):
    yield different_lengths(path, want, have)

  for i, (want_v, have_v) in enumerate(zip(want, have)):
     for difference in diff("%s[%d]" % (path, i), want_v, have_v):
       yield difference


def diff_dicts(path, want, have):
  for k, want_v in want.iteritems():
    key_path = "%s.%s" % (path, k)

    if k not in have:
      yield missing_item(path, k)
    else:
      for difference in diff(key_path, want_v, have[k]):
        yield difference


def diff(path, want, have):
  if isinstance(want, dict):
    for difference in diff_dicts(path, want, have):
      yield difference

  elif isinstance(want, list):
    for difference in diff_lists(path, want, have):
      yield difference

  else:
    if not want == have:
      yield not_equal(path, want, have)


def check_file(printer, path, kubeconfig=None):
  """Check YAML file 'path' for differences.

  :param printer: Where we report differences to.
  :param str path: The YAML file to test.
  :param str kubeconfig: Path to a Kubernetes configuration file.
      If None, we use the default.
  :return: Number of differences found.
  """
  with open(path, 'r') as stream:
    expected = yaml.load(stream)

  kube_obj = KubeObject.from_dict(expected)

  printer.add(kube_obj)

  try:
    running = kube_obj.get_from_cluster(kubeconfig=kubeconfig)
  except subprocess.CalledProcessError, e:
    printer.diff(kube_obj, broken_cluster(e.output))
    return

  differences = 0
  for difference in diff("", expected, running):
    differences += 1
    printer.diff(kube_obj, difference)
  return differences


class StdoutPrinter(object):
  def add(self, kube_obj):
    print "Checking %s '%s'" % (kube_obj.kind, kube_obj.name)

  def diff(self, kube_obj, difference):
    print " *** " + difference.to_text()

  def finish(self):
    pass


class JSONPrinter(object):
  def __init__(self):
    self.data = {}

  def add(self, kube_obj):
    self.data.setdefault(kube_obj.kind, {}).setdefault(kube_obj.name, [])

  def diff(self, kube_obj, difference):
    record = self.data[kube_obj.kind][kube_obj.name]
    record.append([difference.path] + list(difference.args))

  def finish(self):
    print json.dumps(self.data, sort_keys=True, indent=2, separators=(',', ': '))


def check_files(paths, printer, kubeconfig=None):
  """Check all files in 'paths' for differences to a Kubernetes cluster.

  :param printer: Where differences are reported to as they are found.
  :param str kubeconfig: Path to a kubeconfig file for the cluster to diff
      against.
  :return: True if there are differences, False otherwise.
  """
  differences = 0
  for path in iter_files(paths):
    _, extension = os.path.splitext(path)
    if extension != ".yaml":
      continue
    differences += check_file(printer, path, kubeconfig=kubeconfig)

  printer.finish()
  return bool(differences)
