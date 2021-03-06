"""
Zync Maya Plugin

This Maya plugin implements the Zync Python API to provide an interface
for launching Maya jobs on Zync.

Depends on the zync-python Python API:

https://github.com/zync/zync-python

Usage:
  import zync_maya
  zync_maya.submit_dialog()

"""

__version__ = '1.5.7'


import base64
import copy
import functools
import glob
import itertools
import math
import os
import re
import string
import sys
import traceback
import types
import webbrowser

import maya_common
import renderman_maya

zync = None
renderman = renderman_maya.Renderman()

VRAY_ENGINE_NAME_CPU = 'cpu'  # 0
VRAY_ENGINE_NAME_OPENCL = 'opencl'  # 1
VRAY_ENGINE_NAME_CUDA = 'cuda'  # 2
VRAY_ENGINE_NAME_UNKNOWN = 'unknown'
RENDER_LABEL_VRAY_CUDA = 'V-Ray (CUDA)'

RENDERER_NAMES = {
    'vray': 'V-Ray',
    'arnold': 'Arnold',
    'renderman': 'Renderman',
    'redshift': 'Redshift',
}

TOKEN_TO_PATTERN_MAP = {
    '<f>': '<f>',
    '<layer>': '<layer>',
    '<udim>': '<udim>',
    '<tile>': '<tile>',
    '<uvtile>': '<uvtile>',
    'u<u>_v<v>': '<u>|<v>',
    '<u>_<v>': '<u>|<v>',
    '<frame0': r'<frame0\d+>',
    '<frame>': '<frame>',
    '<attr:': None,
    '<shapename>': '<shapename>',
}

REDSHIFT_CACHE_ATTRIBUTES = ['irradianceCacheFilename', 'irradiancePointCloudFilename', 'photonFilename', 'subsurfaceScatteringFilename']
REDSHIFT_OCIO_ATTRIBUTES = ['clrMgmtOcioFilename', 'lutFilename']

class NamePrefixAttributes(object):
  arnold = 'defaultRenderGlobals.imageFilePrefix'
  sw = 'defaultRenderGlobals.imageFilePrefix'
  mr = 'defaultRenderGlobals.imageFilePrefix'
  vray = 'vraySettings.fileNamePrefix'
  redshift = 'defaultRenderGlobals.imageFilePrefix'

  @classmethod
  def get_prefix(cls, renderer):
    return getattr(cls, renderer)


def show_exceptions(func):
  """Error-showing decorator for all entry points

  Catches all exceptions and shows them on the screen and in console before
  re-raising. Uses `exception_already_shown` attribute to prevent showing
  the same exception twice.
  """
  @functools.wraps(func)
  def wrapped(*args, **kwargs):
    try:
      return func(*args, **kwargs)
    except Exception as e:
      if not getattr(e, 'exception_already_shown', False):
        traceback.print_exc()
        cmds.confirmDialog(title='Error', message=unicode(e.message), button='OK',
                           defaultButton='OK', icon='critical')
        e.exception_already_shown = True
      raise
  return wrapped

# Importing zync-python is deferred until user's action (i.e. attempt
# to open plugin window), because we are not able to reliably show message
# windows any time earlier. Zync-python is not needed for plugin to load.
@show_exceptions
def import_zync_python():
  """Imports zync-python"""
  global zync
  if zync:
    return

  if os.environ.get('ZYNC_API_DIR'):
    API_DIR = os.environ.get('ZYNC_API_DIR')
  else:
    config_path = os.path.join(os.path.dirname(__file__), 'config_maya.py')
    if not os.path.exists(config_path):
      raise maya_common.MayaZyncException(
        "Plugin configuration incomplete: zync-python path not provided.\n\n"
        "Re-installing the plugin may solve the problem.")
    import imp
    config_maya = imp.load_source('config_maya', config_path)
    API_DIR = config_maya.API_DIR
    if not isinstance(API_DIR, basestring):
      raise maya_common.MayaZyncException("API_DIR defined in config_maya.py is not a string")

  sys.path.append(API_DIR)
  import zync


UI_FILE = '%s/resources/submit_dialog.ui' % (os.path.dirname(__file__),)

_VERSION_CHECK_RESULT = None

# a list of Xgen attributes which contain filenames we should include for upload
_XGEN_FILE_ATTRS = [
  'files',
  'wiresFile',
  'cacheFileName',
]

# Valid frame range regexes.
# A single frame, e.g. 47, -4
_SINGLE_FRAME_RE = re.compile(r'^(-?)\d+$')
# A contiguous range of frames, e.g. 1-5, -3-2
_FRAME_RANGE_RE = re.compile(r'^(?P<sf>(-?)\d+)-(?P<ef>(-?)\d+)$')

# Regex for finding a frame number in a file path.
_FRAME_NUMBER_RE = re.compile(r'.+\.(?P<frame>[0-9]+)\..+')

# Regex string for checking if string contains a layer token.
_HAS_LAYER_TOKEN_RE = re.compile(r'.*%l.*|.*<layer>.*|.*<renderlayer>.*', re.IGNORECASE)
_SUBSTITUTE_LAYER_TOKEN_RE = re.compile(r'%l|<layer>|<renderlayer>', re.IGNORECASE)
_SUBSTITUTE_CAMERA_TOKEN_RE = re.compile(r'%c|<camera>', re.IGNORECASE)
_SUBSTITUTE_SCENE_TOKEN_RE = re.compile(r'%s|<scene>', re.IGNORECASE)

# Pairs of attributes which define possible Bifrost cache locations.
_BIFROST_CACHE_PATH_ATTRS = (
  ('guideCachePath', 'guideCacheFileName'),
  ('liquidCachePath', 'liquidCacheFileName'),
  ('liquidmeshCachePath', 'liquidmeshCacheFileName'),
  ('solidCachePath', 'solidCacheFileName'),
)


_XGEN_IMPORT_ERROR = None
_RENDERSETUP_IMPORT_ERROR = None

import maya.cmds as cmds
import maya.mel
import maya.utils
# Attempt to import Xgen API. Log error on failure but continue, in
# case of older Maya version or if Xgen is simply unavailable for
# some reason.
try:
  import xgenm
except ImportError as e:
  _XGEN_IMPORT_ERROR = str(e)
  print 'Error loading Xgen API: %s' % _XGEN_IMPORT_ERROR
# Only newer versions of Maya have the Render Setup API.
try:
  import maya.app.renderSetup.model.renderSetup as renderSetup
except (ImportError, RuntimeError) as e:
  _RENDERSETUP_IMPORT_ERROR = str(e)
  print 'Error loading Render Setup API: %s' % _RENDERSETUP_IMPORT_ERROR


def eval_ui(path, ui_type='textField', **kwargs):
  """
  Returns the value from the given ui element.
  """
  return getattr(cmds, ui_type)(path, query=True, **kwargs)


def proj_dir():
  """
  Returns the Maya project directory of the current scene.
  """
  return cmds.workspace(q=True, rd=True)


def frame_range():
  """
  Returns the frame-range of the maya scene as a string, like:
    1001-1350
  """
  start = str(int(cmds.getAttr('defaultRenderGlobals.startFrame')))
  end = str(int(cmds.getAttr('defaultRenderGlobals.endFrame')))
  return '%s-%s' % (start, end)


def get_render_layers():
  """Get a list of all render layers in the scene."""
  layers = []
  try:
    all_layers = cmds.ls(type='renderLayer', showNamespace=True)
    for i in range(0, len(all_layers), 2):
      if all_layers[i+1] == ':':
        layers.append(all_layers[i])
  except Exception:
    layers = cmds.ls(type='renderLayer')
  return layers


def udim_range():
  bake_sets = list(bake_set for bake_set in cmds.ls(type='VRayBakeOptions') \
    if bake_set != 'vrayDefaultBakeOptions')
  u_max = 0
  v_max = 0
  for bake_set in bake_sets:
    conn_list = cmds.listConnections(bake_set)
    if conn_list == None or len(conn_list) == 0:
      continue
    uv_info = cmds.polyEvaluate(conn_list[0], b2=True)
    if uv_info[0][1] > u_max:
      u_max = int(math.ceil(uv_info[0][1]))
    if uv_info[1][1] > v_max:
      v_max = int(math.ceil(uv_info[1][1]))
  return '1001-%d' % (1001+u_max+(10*v_max))


def seq_to_glob(in_path):
  """Takes an image sequence path and returns it in glob format.

  Any frame numbers or other tokens will be replaced by a '*'.
  Image sequences may be numerical sequences, e.g. /path/to/file.1001.exr
  will return as /path/to/file.*.exr. Image sequences may also use tokens to
  denote sequences, e.g. /path/to/texture.<UDIM>.tif will return as
  /path/to/texture.*.tif.

  Args:
    in_path: str, the image sequence path

  Returns:
    String, the new path, subbed with any needed wildcards.
  """
  if in_path is None:
    return in_path
  in_path = _replace_attr_tokens(in_path)
  in_path = re.sub('<meshitem>', '*', in_path, flags=re.IGNORECASE)

  found_token = False
  if '#' in in_path:
    in_path = re.sub('#+', '*', in_path, flags=re.IGNORECASE)
    found_token = True
  for token in TOKEN_TO_PATTERN_MAP:
    if token in in_path.lower() and TOKEN_TO_PATTERN_MAP[token] is not None:
      in_path = re.sub(TOKEN_TO_PATTERN_MAP[token], '*', in_path, flags=re.IGNORECASE)
      found_token = True
  if found_token:
    return in_path

  head = os.path.dirname(in_path)
  base = os.path.basename(in_path)
  matches = list(re.finditer(r'\d+', base))
  if matches:
    match = matches[-1]
    new_base = '%s*%s' % (base[:match.start()], base[match.end():])
    return '%s/%s' % (head, new_base)
  else:
    return in_path


def _replace_attr_tokens(path):
  if not path:
    return path
  glob_path = re.sub(r'<attr:.*?>', '*', path, flags=re.IGNORECASE)
  if not re.search(r'[^/*]', glob_path):
    raise maya_common.MayaZyncException(
        'A file path using attr: tags resolved to %s, which is too wide. '
        'Please use attr: tags only for portions of the file path to limit the '
        'potential matches for these paths; this will help both Arnold and '
        'Zync locate these files.' % glob_path)
  return glob_path


def get_file_node_path(node):
  """Get the file path used by a Maya file node.
  Args:
    node: str, name of the Maya file node
  Returns:
    str, the file path in use
  """
  # if the path appears to be sequence, use computedFileTextureNamePattern,
  # this preserves the <> tag
  if cmds.attributeQuery('computedFileTextureNamePattern', node=node, exists=True):
    textureNamePattern = cmds.getAttr('%s.computedFileTextureNamePattern' % node)
    if any(token in textureNamePattern.lower() for token in TOKEN_TO_PATTERN_MAP):
      return cmds.getAttr('%s.computedFileTextureNamePattern' % node)
  # otherwise use fileTextureName
  return cmds.getAttr('%s.fileTextureName' % node)


def node_uses_image_sequence(node):
  """Determine if a node uses an image sequence or just a single image,
  not always obvious from its file path alone.
  Args:
    node: str, name of the Maya node
  Returns:
    bool, True if node uses an image sequence
  """
  # useFrameExtension indicates an explicit image sequence
  # a <UDIM> token implies a sequence
  node_path = get_file_node_path(node).lower()
  return (cmds.getAttr('%s.useFrameExtension' % node) == True or
      any(token in node_path for token in TOKEN_TO_PATTERN_MAP))


def _get_layer_overrides(attr):
  """Gets any files set in layer overrides linked to the given attribute.

  Args:
    attr: str, Maya attribute name, like file1.fileTextureName

  Yields:
    the value of any render layer overrides. this can be a str,
    int, float - it depends on what type the attr itself is.
  """
  connections = cmds.listConnections(attr, plugs=True)
  # listConnections can return None if there are no connections
  if connections:
    for connection in connections:
      # listConnections gives us any "plugs" which are connected to
      # the attribute. a plug represents the connection, not the actual
      # value of the override. a plug is a str, like:
      #   layer1.adjustments[1].plug
      if connection:
        # we only care when the plug refers to a render layer, as it
        # will represent a render layer override.
        node_name = connection.split('.')[0]
        if cmds.nodeType(node_name) == 'renderLayer':
          # turn the plug name into a value name, which looks
          # like: layer1.adjustments[1].value
          attr_name = '%s.value' % '.'.join(connection.split('.')[:-1])
          yield cmds.getAttr(attr_name)


def _file_handler(node):
  """Returns the file referenced by a Maya file node. Returned files may
  contain wildcards when they reference image sequences, for example an
  animated texture node, or a path containing <UDIM> token."""
  texture_path = get_file_node_path(node)
  # if the node is an image sequence, transform the path into a
  # glob-style path, i.e. using * in place of any sequence number
  # or token. this will match what's provided via the file list
  # in the job's scene_info, so we can properly path swap
  if node_uses_image_sequence(node):
    texture_path = seq_to_glob(texture_path)
  yield texture_path
  # if the Arnold "Use .tx" flag is on, look for a .tx version
  # of the texture as well
  try:
    if cmds.getAttr('defaultArnoldRenderOptions.use_existing_tiled_textures'):
      head, _ = os.path.splitext(texture_path)
      yield '%s.tx' % head
  except:
    pass
  # look for layer overrides set on the path
  for override_path in _get_layer_overrides('%s.fileTextureName' % node):
    yield override_path


def _cache_file_handler(node):
  """Returns the files references by the given cacheFile node"""
  path = cmds.getAttr('%s.cachePath' % node)
  cache_name = cmds.getAttr('%s.cacheName' % node)

  yield '%s/%s*.mc' % (path, cache_name)
  yield '%s/%s*.mcc' % (path, cache_name)
  yield '%s/%s*.mcx' % (path, cache_name)
  yield '%s/%s.xml' % (path, cache_name)


def _diskCache_handler(node):
  """Given a diskCache node, returns path of cache file it
  references.

  Args:
    node: str, name of diskCache node

  Yields:
    tuple of str, paths referenced
  """
  cache_name = cmds.getAttr('%s.cacheName' % node)
  # if its an absolute path we're done, otherwise we need to resolve it
  # via project settings
  if os.path.isabs(cache_name):
    yield cache_name
  else:
    disk_cache_dir = cmds.workspace(fileRuleEntry='diskCache')
    if not disk_cache_dir:
      print 'WARNING: disk cache path not found. assuming data/'
      disk_cache_dir = 'data'
    # resolve relative paths with the main project path
    if not os.path.isabs(disk_cache_dir):
      disk_cache_dir = os.path.join(cmds.workspace(q=True, rd=True),
                                    disk_cache_dir)
    yield os.path.join(disk_cache_dir, cache_name)


def _vrmesh_handler(node):
  """Handles vray meshes"""
  yield cmds.getAttr('%s.fileName' % node)


def _mrtex_handler(node):
  """Handles mentalrayTexutre nodes"""
  yield cmds.getAttr('%s.fileTextureName' % node)


def _gpu_handler(node):
  """Handles gpuCache nodes"""
  yield cmds.getAttr('%s.cacheFileName' % node)


def _mrOptions_handler(node):
  """Handles mentalrayOptions nodes, for Final Gather"""
  mapName = cmds.getAttr('%s.finalGatherFilename' % node).strip()
  if mapName != "":
    path = cmds.workspace(q=True, rd=True)
    if path[-1] != "/":
      path += "/"
    path += "renderData/mentalray/finalgMap/"
    path += mapName
    #if not mapName.endswith(".fgmap"):
    #   path += ".fgmap"
    path += "*"
    yield path


def _mrIbl_handler(node):
  """Handles mentalrayIblShape nodes"""
  yield cmds.getAttr('%s.texture' % node)


def _abc_handler(node):
  """Handles AlembicNode nodes"""
  yield cmds.getAttr('%s.abc_File' % node)


def _vrSettings_handler(node):
  """Handles VRaySettingsNode nodes, for irradiance map"""
  irmap = cmds.getAttr('%s.ifile' % node)
  if cmds.getAttr('%s.imode' % node) == 7:
    if irmap.find('.') == -1:
      irmap += '*'
    else:
      last_dot = irmap.rfind('.')
      irmap = '%s*%s' % (irmap[:last_dot], irmap[last_dot:])
  yield irmap
  yield cmds.getAttr('%s.fnm' % node)


def _particle_handler(node):
  project_dir = cmds.workspace(q=True, rd=True)
  if project_dir[-1] == '/':
    project_dir = project_dir[:-1]
  if node.find('|') == -1:
    node_base = node
  else:
    node_base = node.split('|')[-1]
  path = None
  try:
    startup_cache = cmds.getAttr('%s.scp' % (node,)).strip()
    if startup_cache in (None, ''):
      path = None
    else:
      path = '%s/particles/%s/%s*' % (project_dir, startup_cache, node_base)
  except:
    path = None
  if path == None:
    scene_base, ext = os.path.splitext(os.path.basename(cmds.file(q=True, loc=True)))
    path = '%s/particles/%s/%s*' % (project_dir, scene_base, node_base)
  yield path


def _ies_handler(node):
  """Handles VRayLightIESShape nodes, for IES lighting files"""
  yield cmds.getAttr('%s.iesFile' % node)


def _fur_handler(node):
  """Handles FurDescription nodes"""
  #
  #  Find all "Map" attributes and see if they have stored file paths.
  #
  for attr in cmds.listAttr(node):
    if attr.find('Map') != -1 and cmds.attributeQuery(attr, node=node, at=True) == 'typed':
      index_list = ['0', '1']
      for index in index_list:
        try:
          map_path = cmds.getAttr('%s.%s[%s]' % (node, attr, index))
          if map_path != None and map_path != '':
            yield map_path
        except:
          pass


def _ptex_handler(node):
  """Handles Mental Ray ptex nodes"""
  yield cmds.getAttr('%s.S00' % node)


def _substance_handler(node):
  """Handles Vray Substance nodes"""
  yield cmds.getAttr('%s.p' % node)


def _imagePlane_handler(node):
  """Handles Image Planes"""
  # only return the path if the display mode is NOT set to "None"
  if cmds.getAttr('%s.displayMode' % (node,)) != 0:
    texture_path = cmds.getAttr('%s.imageName' % (node,))
    try:
      if cmds.getAttr('%s.useFrameExtension' % (node,)) == True:
        yield seq_to_glob(texture_path)
      else:
        yield texture_path
    except:
      yield texture_path


def _mesh_handler(node):
  """Handles Mesh nodes, in case they are using MR Proxies"""
  for attr in ['%s.miProxyFile', '%s.rman__param___draFile']:
    try:
      proxy_path = cmds.getAttr(attr % node)
      if proxy_path != None:
        yield proxy_path
    except:
      pass


def _dynGlobals_handler(node):
  """Handles dynGlobals nodes"""
  project_dir = cmds.workspace(q=True, rd=True)
  if project_dir[-1] == '/':
    project_dir = project_dir[:-1]
  cache_dir = cmds.getAttr('%s.cd' % (node,))
  if cache_dir not in (None, ''):
    path = '%s/particles/%s/*' % (project_dir, cache_dir.strip())
    yield path


def _aiStandIn_handler(node):
  """Handles aiStandIn nodes"""
  path = cmds.getAttr('%s.dso' % (node,))
  # change frame reference to wildcard pattern
  yield seq_to_glob(path)


def _aiImage_handler(node):
  """Handles aiImage nodes"""
  filename = cmds.getAttr('%s.filename' % node)
  yield seq_to_glob(filename)


def _aiPhotometricLight_handler(node):
  """Handles aiPhotometricLight nodes"""
  yield cmds.getAttr('%s.aiFilename' % node)


def _exocortex_handler(node):
  """Handles Exocortex Alembic nodes"""
  yield cmds.getAttr('%s.fileName' % node)


def _vrayPtex_handler(node):
  yield cmds.getAttr('%s.ptexFile' % node)


def _vrayVolumeGrid_handler(node):
  path = cmds.getAttr('%s.if' % node)
  yield seq_to_glob(path)


def _vrayScene_handler(node):
  vrscene_path = cmds.getAttr('%s.fPath' % node)
  yield vrscene_path
  # Scan the .vrscene file for dependencies buried within.
  # If the file does not exist we skip the scan but still report the main
  # .vrscene file dependency to Zync, this is to allow Zync's default
  # missing file detection to kick in when the job runs.
  if os.path.exists(vrscene_path):
    with open(vrscene_path) as fp:
      for vrscene_line in fp:
        # Files in the .vrscene are attached to nodes like this:
        # BitmapBuffer bitmapBuffer_1 {
        #   file="/path/to/file.exr";
        vrscene_line = vrscene_line.strip()
        if vrscene_line.startswith('file='):
          file_path = '='.join(vrscene_line.split('=')[1:])
          file_path = file_path.strip(';')
          file_path = file_path.strip('\'"')
          yield file_path


def _openVDBRead_handler(node):
  """Handles OpenVDBRead nodes"""
  yield cmds.getAttr('%s.file' % node)


def _aiVolume_handler(node):
  """Handles aiVolume nodes, Arnold volume grid files."""
  yield seq_to_glob(cmds.getAttr('%s.filename' % node))


def _mash_handler(node):
  archive_paths = cmds.getAttr('%s.ribArchives' % node)
  if archive_paths:
    for archive_path in archive_paths.split(','):
      yield archive_path


def _mashAudio_handler(node):
  yield cmds.getAttr('%s.filename' % node)


def _bifrost_handler(frames_to_render, bifrost_container):
  cache_paths = set()
  for cache_path_attr, cache_name_attr in _BIFROST_CACHE_PATH_ATTRS:
    container_attrs = cmds.listAttr(bifrost_container)
    if cache_path_attr in container_attrs and cache_name_attr in container_attrs:
      cache_path = cmds.getAttr('%s.%s' % (bifrost_container, cache_path_attr))
      cache_name = cmds.getAttr('%s.%s' % (bifrost_container, cache_name_attr))
      if cache_path and cache_name:
        cache_paths.add(os.path.join(cache_path, cache_name))

  for cache_directory in cache_paths:
    for cache_file in glob.glob('%s/*/*' % cache_directory):
      frame_num = extract_frame_number_from_file_path(cache_file)
      if frame_num is None:
        yield cache_file
      else:
        if frame_num in frames_to_render:
          yield cache_file


def get_redshift_version():
  return str(cmds.pluginInfo('redshift4maya', query=True, version=True))


def generate_redshift_asset_paths():
  for path in cmds.file(q=True, list=True):
    yield seq_to_glob(path)

  for path in generate_redshift_layer_overriden_paths('RedshiftOptions', REDSHIFT_CACHE_ATTRIBUTES):
    yield path

  for path in generate_redshift_layer_overriden_paths('RedshiftPostEffects', REDSHIFT_OCIO_ATTRIBUTES):
    if path:
      # OCIO files
      if path.lower().endswith('.ocio'):
        for ocio_file in zync.get_ocio_files(path):
          yield ocio_file
      else:
        yield path


def generate_redshift_layer_overriden_paths(node_type, attributes):
  for node in cmds.ls(type=node_type):
    for attr in attributes:
      for layer in get_render_layers():
        path = get_layer_override(layer, 'redshift', '%s.%s' % (node, attr))
        yield seq_to_glob(path)


def generate_redshift_second_order_dependency_paths(frame_numbers):
  for proxy_node in cmds.ls(type='RedshiftProxyMesh'):
    proxy_path = cmds.getAttr('%s.computedFileNamePattern' % proxy_node)
    if cmds.getAttr('%s.useFrameExtension' % proxy_node):
      proxy_paths = [replace_frame_number(proxy_path, frame) for frame in frame_numbers]
    else:
      proxy_paths = [proxy_path]
    for path in proxy_paths:
      for proxy_asset in maya.mel.eval('rsProxy -q -dependencies "%s"' % path):
        yield _clean_path(proxy_asset)


def replace_frame_number(path, frame_number):
  frame_number = int(frame_number)
  if frame_number < 0:
    raise ValueError("Frame number must be non-negative")

  def split_by_token(_path):
    return re.split('(#+)', _path)

  def is_token(_part):
    return '#' in _part

  def replace_token(_path, _frame_number):
    _frame_number = str(_frame_number)
    if len(_frame_number) >= len(_path):
      return _frame_number
    num_of_zeroes = len(_path) - len(_frame_number)
    return (num_of_zeroes*'0') + _frame_number

  parts = split_by_token(path)
  final_path = ""
  for part in parts:
    final_part = part
    if is_token(part):
      final_part = replace_token(part, frame_number)
    final_path += final_part
  return final_path


def get_scene_files(frames_to_render, renderer):
  generator = itertools.chain()
  # Generate asset paths
  if renderer == 'redshift':
    generator = itertools.chain(generator, generate_redshift_asset_paths())
  else:
    generator = itertools.chain(generator, generate_asset_paths(frames_to_render))

  # Generate recursive dependency paths
  if not eval_ui('ignore_second_deps', 'checkBox', v=True):
    if renderer == 'renderman':
      generator = itertools.chain(generator, renderman.generate_second_order_dependency_paths())
    elif renderer == 'redshift':
      generator = itertools.chain(generator, generate_redshift_second_order_dependency_paths(frames_to_render))

  # Generate xgen paths
  generator = itertools.chain(generator, generate_xgen_paths())

  # Handle OCIO dependencies
  generator = itertools.chain(generator, generate_ocio_files())
  return list(generator)


def generate_asset_paths(frames_to_render):
  """Returns all of the files being used by the scene"""
  file_types = {
    'file': _file_handler,
    'cacheFile': _cache_file_handler,
    'diskCache': _diskCache_handler,
    'VRayMesh': _vrmesh_handler,
    'mentalrayTexture': _mrtex_handler,
    'gpuCache': _gpu_handler,
    'mentalrayOptions': _mrOptions_handler,
    'mentalrayIblShape': _mrIbl_handler,
    'AlembicNode': _abc_handler,
    'VRaySettingsNode': _vrSettings_handler,
    'particle': _particle_handler,
    'VRayLightIESShape': _ies_handler,
    'FurDescription': _fur_handler,
    'mib_ptex_lookup': _ptex_handler,
    'substance': _substance_handler,
    'imagePlane': _imagePlane_handler,
    'mesh': _mesh_handler,
    'dynGlobals': _dynGlobals_handler,
    'aiStandIn': _aiStandIn_handler,
    'aiImage': _aiImage_handler,
    'aiPhotometricLight': _aiPhotometricLight_handler,
    'ExocortexAlembicFile': _exocortex_handler,
    'VRayPtex': _vrayPtex_handler,
    'VRayVolumeGrid': _vrayVolumeGrid_handler,
    'VRayScene': _vrayScene_handler,
    'RenderManArchive': renderman.ribArchive_handler,
    'PxrStdEnvMapLight': renderman.pxrStdEnvMap_handler,
    'PxrDomeLight': renderman.pxrDomeLight_handler,
    'PxrTexture': renderman.pxrTexture_handler,
    'PxrBump': renderman.pxrTexture_handler, # PxrBump and PxrTexture are identical.
    'PxrMultiTexture': renderman.pxrMultiTexture_handler,
    'PxrDomeLight': renderman.pxrDomeLight_handler,
    'RMSEnvLight': renderman.rmsEnvLight_handler,
    'PxrPtexture': renderman.pxrPtexture_handler,
    'PxrNormalMap': renderman.pxrNormalMap_handler,
    'OpenVDBRead': _openVDBRead_handler,
    'aiVolume': _aiVolume_handler,
    'MASH_Waiter': _mash_handler,
    'MASH_Audio': _mashAudio_handler,
    'bifrostContainer': functools.partial(_bifrost_handler, frames_to_render),
  }

  for file_type in file_types:
    handler = file_types.get(file_type)
    nodes = cmds.ls(type=file_type)
    for node in nodes:
      for scene_file in handler(node):
        if scene_file:
          scene_file = scene_file.replace('\\', '/')
          print 'found file dependency from %s node %s: %s' % (file_type, node, scene_file)
          yield scene_file


def generate_xgen_paths():
  try:
    for xgen_file in get_xgen_files():
      yield xgen_file
  except NameError as e:
    print 'error retrieving Xgen file list: %s' % str(e)


def get_xgen_files():
  """Yield all Xgen file dependencies in the scene."""
  # Get collection list, if the call fails due to Xgen not being
  # loaded, stop.
  if _XGEN_IMPORT_ERROR:
    raise NameError('Xgen is not loaded due to error: %s' % _XGEN_IMPORT_ERROR)
  # try to get collection list using uiPalettes() instead of the standard
  # xgenm.palettes() because the latter can pick up temp collections
  # which aren't needed and sometimes don't actually exist.
  try:
    collection_list = xgenm.ui.util.xgUtil.uiPalettes()
  # sometimes xgenm.ui doesn't exist, if the user does not have the Xgen
  # plugin loaded. in this case, fall back to the old way of getting
  # collections. it's unlikely this will return any of the abovementioned
  # temporary collections, or anything at all, because the user will
  # have the Xgen plugin loaded if they are using Xgen.
  except AttributeError:
    collection_list = xgenm.palettes()
  for collection in collection_list:
    for def_file in _get_xgen_collection_definition(collection):
      print 'found Xgen collection definition: %s' % def_file
      yield def_file
    for xgen_file in _get_xgen_collection_files(collection):
      print 'found Xgen collection file: %s' % xgen_file
      yield xgen_file


def _get_xgen_collection_definition(collection_name):
  """Yield Xgen collection direct dependencies.

  Args:
    collection_name: str, name of Xgen collection in the current scene

  Returns:
    Yields str for each definition files associated with that collection.
  """
  if _XGEN_IMPORT_ERROR:
    raise NameError('Xgen is not loaded due to error: %s' % _XGEN_IMPORT_ERROR)
  scene_dir, scene_basename = os.path.split(cmds.file(q=True, loc=True))
  scene_name, _ = os.path.splitext(scene_basename)
  # Xgen definition files must meet very specific conventions - they
  # must live in the same directory as the scene file and be named
  # according to a strict <scene name>__<collection name> format.
  # These are Xgen conventions, not specific to Zync.
  # Maya avoids using the namespace character ':' in filenames, so
  # we must do the same replacement.
  filenames = [
    '%s__%s.xgen' % (scene_name, collection_name.replace(':', '__')),
    '%s__%s.abc' % (scene_name, collection_name.replace(':', '__ns__')),
  ]
  for filename in filenames:
    yield os.path.join(scene_dir, filename).replace('\\', '/')


def _get_xgen_collection_files(collection_name):
  """Get Xgen indirect dependencies, specifically files stored
  in related objects."""
  if _XGEN_IMPORT_ERROR:
    raise NameError('Xgen is not loaded due to error: %s' % _XGEN_IMPORT_ERROR)
  xg_proj_path = xgenm.getAttr('xgProjectPath', collection_name)
  xg_data_path = xgenm.getAttr('xgDataPath', collection_name)
  xg_data_path = xg_data_path.replace('${PROJECT}', xg_proj_path)
  # upload all files under collection root
  for dir_name, subdir_list, file_list in os.walk(xg_data_path):
    for xg_file in file_list:
      if not xg_file.startswith('.'):
        yield os.path.join(dir_name, xg_file).replace('\\', '/')
  # search objects for files too
  for xg_desc in xgenm.descriptions(collection_name):
    obj_list = (xgenm.objects(collection_name, xg_desc) +
                xgenm.fxModules(collection_name, xg_desc))
    for xg_obj in obj_list:
      for xg_file in _get_xgen_object_files(collection_name, xg_desc, xg_obj):
        yield xg_file


def _get_xgen_object_files(collection_name, desc_name, object_name):
  """Get all files linked to an Xgen object."""
  if _XGEN_IMPORT_ERROR:
    raise NameError('Xgen is not loaded due to error: %s' % _XGEN_IMPORT_ERROR)
  # the "files" attr requires some special parsing, handle this first
  if xgenm.attrExists('files', collection_name, desc_name, object_name):
    for file_path in _get_files_from_files_attr(collection_name, desc_name,
                                                object_name):
      yield file_path
  # look for other attributes which are expected to contain file paths
  for file_attr in _XGEN_FILE_ATTRS:
    # "files" attr is already handled above
    if file_attr == 'files':
      continue
    if xgenm.attrExists(file_attr, collection_name, desc_name, object_name):
      yield xgenm.getAttr(file_attr, collection_name, desc_name, object_name)
  # search other attributes for file paths
  for other_attr_file in _get_files_from_other_attrs(collection_name, desc_name,
                                                     object_name):
      yield other_attr_file


def _get_files_from_files_attr(collection_name, desc_name, object_name):
  """Get all files stored in the "files" attribute of an Xgen object."""
  xg_proj_path = xgenm.getAttr('xgProjectPath', collection_name)
  # files attr has a rather strange format, which we must parse and attempt
  # to infer file paths from. For example:
  # #ArchiveGroup 0 name="stalagmite" thumbnail="stalagmite.png" description="No description." \
  #   materials="${PROJECT}/xgen/archives/materials/stalagmite.ma" color=[1.0,0.0,0.0]\n0 \
  #   "${PROJECT}/xgen/archives/abc/stalagmite.abc"
  for attr in re.findall(r'(?:[^\s,"]|"(?:\\.|[^"])*")+',
      xgenm.getAttr('files', collection_name, desc_name, object_name)):
    attr_split = attr.split('=')
    current_file = None
    if not attr_split:
      pass
    # Look for something that looks like a file path
    elif len(attr_split) < 2 and ('/' in attr or '\\' in attr):
      current_file = attr.strip('"').replace('${PROJECT}', xg_proj_path)
    # Also catch materials= tags.
    elif attr_split[0] == 'materials':
      current_file = attr_split[1].strip('"').replace('${PROJECT}', xg_proj_path)
    if current_file:
      yield current_file
      # If the file is a .gz archive, look for a toc file as well. Arnold archives
      # in particular often require this.
      if current_file.endswith('.gz'):
        head, _ = os.path.splitext(current_file)
        toc_path = head + 'toc'
        if os.path.exists(toc_path):
          yield toc_path


def _get_files_from_other_attrs(collection_name, desc_name, object_name):
  """Searches attributes of an Xgen object to try to detect file paths."""
  for attr in xgenm.attrs(collection_name, desc_name, object_name):
    # skip any attrs which probably contain plain file paths, which we've
    # already collected above
    if attr in _XGEN_FILE_ATTRS:
      continue
    attr_val = xgenm.getAttr(attr, collection_name, desc_name, object_name)
    # the map() directive indicates an Xgen expression which reads in an image
    # map and applies derives the attribute value from that image data, much
    # like a texture map.
    if 'map(' in attr_val:
      opening_paren = attr_val.find('map(') + 4
      closing_paren = _find_matching_paren(attr_val, opening_paren)
      # if no matching paren was found just skip this attr - its probably a
      # broken expression or a bit of code left in a comment
      if closing_paren is None:
        continue
      file_path = attr_val[opening_paren:closing_paren].strip('"').strip("'")
      # if the path starts with $, that means its path is based on an Xgen
      # variable, usually ${DESC}. this means it is stored within the
      # collection directory structure, and would have already been collected
      # above
      if file_path.startswith('$'):
        continue
      # if its a file, yield it. if its a directory, recurse into the directory
      # and yield all files contained within
      if os.path.isfile(file_path):
        yield file_path.replace('\\', '/')
      elif os.path.isdir(file_path):
        for child_dir, subdir_list, file_list in os.walk(file_path):
          for child_file in file_list:
              yield os.path.join(child_dir, child_file).replace('\\', '/')


def _find_matching_paren(some_string, opening_paren):
  """Given a string and the position of an opening paren, returns the position
  of the corresponding closing paren.

  Args:
    some_string: str, the string which contains the parens
    opening_paren: int, index within some_string of the opening paren

  Returns:
    int, position of the corresponding closing paren, or None if none was found.
  """
  current_open_parens = 0
  for i in range(opening_paren + 1, len(some_string)):
    if some_string[i] == '(':
      current_open_parens += 1
    elif some_string[i] == ')':
      if current_open_parens == 0:
        return i
      else:
        current_open_parens -= 1
  return None


def generate_ocio_files():
  """ Yields external OCIO config file and its dependencies, if enabled. """
  if cmds.colorManagementPrefs(q=True,  cmEnabled=True) and cmds.colorManagementPrefs(q=True, cmConfigFileEnabled=True):
    for ocio_file in zync.get_ocio_files(str(cmds.colorManagementPrefs(q=True,  configFilePath=True))):
      yield ocio_file


def get_default_extension(renderer):
  """
  Returns the filename prefix for the given renderer, either mental ray
  or maya software.
  """
  if renderer == 'sw':
    menu_grp = 'imageMenuMayaSW'
  elif renderer == 'mr':
    menu_grp = 'imageMenuMentalRay'
  else:
    raise Exception('Invalid Renderer: %s' % renderer)
  try:
    val = cmds.optionMenuGrp(menu_grp, q=True, v=True)
  except RuntimeError:
    msg = 'Please open the Maya Render globals before submitting.'
    raise Exception(msg)
  else:
    return val.split()[-1][1:-1]


LAYER_INFO = {}
def collect_layer_info(layer, renderer):
  cur_layer = cmds.editRenderLayerGlobals(q=True, currentRenderLayer=True)
  _switch_to_renderlayer(layer)

  layer_info = {}

  # get list of active render passes
  layer_info['render_passes'] = []
  if (renderer == 'vray' and
    cmds.getAttr('vraySettings.imageFormatStr') != 'exr (multichannel)'
    and cmds.getAttr('vraySettings.relements_enableall') != False):
    pass_list = cmds.ls(type='VRayRenderElement')
    pass_list += cmds.ls(type='VRayRenderElementSet')
    for r_pass in pass_list:
      if cmds.getAttr('%s.enabled' % (r_pass,)) == True:
        layer_info['render_passes'].append(r_pass)
  elif renderer == 'redshift':
    for options_node in cmds.ls(type='RedshiftOptions'):
      for attr in REDSHIFT_CACHE_ATTRIBUTES:
        node_attr = '{0}.{1}'.format(options_node, attr)
        layer_info[node_attr] = cmds.getAttr(node_attr)
    for options_node in cmds.ls(type='RedshiftPostEffects'):
      for attr in REDSHIFT_OCIO_ATTRIBUTES:
        node_attr = '{0}.{1}'.format(options_node, attr)
        layer_info[node_attr] = cmds.getAttr(node_attr)
  try:
    layer_info['prefix'] = cmds.getAttr(NamePrefixAttributes.get_prefix(renderer))
  except Exception:
    layer_info['prefix'] = ''

  _switch_to_renderlayer(cur_layer)
  return layer_info


def clear_layer_info():
  global LAYER_INFO
  LAYER_INFO = {}


def get_layer_override(layer, renderer, field):
  global LAYER_INFO
  if layer not in LAYER_INFO:
    LAYER_INFO[layer] = collect_layer_info(layer, renderer)
  return LAYER_INFO[layer][field]


def get_maya_version():
  """Returns the current major Maya version in use."""
  # `about -api` returns a value containing both major and minor
  # maya versions in one integer, e.g. 201515. Divide by 100 to
  # find the major version.
  version_full = maya.mel.eval('about -api') / 100.0
  # Maya 2018 added two additional digits to the API version.
  if float(version_full) >= 201800:
    version_full /= 100.0
  # Maya 2016 rounds down to the nearest .5
  if int(version_full) == 2016:
    version_rounded = math.floor(version_full * 2) / 2
  # Other versions round down to the nearest whole version.
  else:
    version_rounded = math.floor(version_full)
  # if it's a whole number e.g. 2016.0, drop the decimal
  if version_rounded.is_integer():
    version_rounded = int(version_rounded)
  return str(version_rounded)


def get_scene_info(renderer, layers_to_render, is_bake, extra_assets, frames_to_render):
  """Returns scene info for the current scene.

  Args:
    renderer: str, the renderer that will be used - some info returned is
              renderer-specific
    layers_to_render: [str], list of render layers that will be rendered
    is_bake: bool, whether job is a bake job
    extra_assets: [str], list of any extra files to include
    frames_to_render: [int], list of each frame to be rendered

  Returns:
    dict of scene information
  """
  print '--> initializing'
  clear_layer_info()

  print '--> render layers'
  scene_info = {'render_layers': get_render_layers()}

  print '--> checking selections'
  if is_bake:
    selected_bake_sets = layers_to_render
    if selected_bake_sets == None:
      selected_bake_sets = []
    selected_layers = []
  else:
    selected_layers = layers_to_render
    if selected_layers == None:
      selected_layers = []
    selected_bake_sets = []

  # Detect a list of referenced files. We must use ls() instead of file(q=True, r=True)
  # because the latter will only detect references one level down, not nested references.
  print '--> references'
  scene_info['references'] = []
  scene_info['unresolved_references'] = []
  for ref_node in cmds.ls(type='reference'):
    try:
      scene_info['references'].append(cmds.referenceQuery(ref_node, filename=True))
      scene_info['unresolved_references'].append(
        cmds.referenceQuery(ref_node, filename=True, unresolvedName=True))
    except:
      pass

  print '--> render passes'
  scene_info['render_passes'] = {}
  if renderer == 'vray' and cmds.getAttr('vraySettings.imageFormatStr') != 'exr (multichannel)':
    pass_list = cmds.ls(type='VRayRenderElement')
    pass_list += cmds.ls(type='VRayRenderElementSet')
    if len(pass_list) > 0:
      for layer in selected_layers:
        scene_info['render_passes'][layer] = []
        enabled_passes = get_layer_override(layer, renderer, 'render_passes')
        for r_pass in pass_list:
          if r_pass in enabled_passes:
            vray_name = None
            vray_explicit_name = None
            vray_file_name = None
            for attr_name in cmds.listAttr(r_pass):
              if attr_name.startswith('vray_filename'):
                vray_file_name = cmds.getAttr('%s.%s' % (r_pass, attr_name))
              elif attr_name.startswith('vray_name'):
                vray_name = cmds.getAttr('%s.%s' % (r_pass, attr_name))
              elif attr_name.startswith('vray_explicit_name'):
                vray_explicit_name = cmds.getAttr('%s.%s' % (r_pass, attr_name))
            if vray_file_name != None and vray_file_name != "":
              final_name = vray_file_name
            elif vray_explicit_name != None and vray_explicit_name != "":
              final_name = vray_explicit_name
            elif vray_name != None and vray_name != "":
              final_name = vray_name
            else:
              continue
            # special case for Material Select elements - these are named based on the material
            # they are connected to.
            if 'vray_mtl_mtlselect' in cmds.listAttr(r_pass):
              connections = cmds.listConnections('%s.vray_mtl_mtlselect' % (r_pass,))
              if connections:
                final_name += '_%s' % (str(connections[0]),)
            scene_info['render_passes'][layer].append(final_name)

  print '--> bake sets'
  scene_info['bake_sets'] = {}
  for bake_set in selected_bake_sets:
    scene_info['bake_sets'][bake_set] = {
      'uvs': _get_bake_set_uvs(bake_set),
      'map': _get_bake_set_map(bake_set),
      'shape': _get_bake_set_shape(bake_set),
      'output_path': _get_bake_set_output_path(bake_set),
    }

  print '--> frame extension & padding'
  if renderer == 'vray':
    scene_info['extension'] = cmds.getAttr('vraySettings.imageFormatStr')
    if scene_info['extension'] == None:
      scene_info['extension'] = 'png'
    scene_info['padding'] = int(cmds.getAttr('vraySettings.fileNamePadding'))
  elif renderer == 'mr':
    scene_info['extension'] = cmds.getAttr('defaultRenderGlobals.imfPluginKey')
    if not scene_info['extension']:
      scene_info['extension'] = get_default_extension(renderer)
    scene_info['padding'] = int(cmds.getAttr('defaultRenderGlobals.extensionPadding'))
  elif renderer == 'arnold':
    scene_info['extension'] = cmds.getAttr('defaultRenderGlobals.imfPluginKey')
    scene_info['padding'] = int(cmds.getAttr('defaultRenderGlobals.extensionPadding'))
  elif renderer == 'renderman':
    if cmds.getAttr('defaultRenderGlobals.outFormatControl'):
      scene_info['extension'] = cmds.getAttr('defaultRenderGlobals.outFormatExt').lstrip('.')
    else:
      scene_info['extension'] = renderman.get_extension()
    scene_info['padding'] = int(cmds.getAttr('defaultRenderGlobals.extensionPadding'))
  elif renderer == 'redshift':
    scene_info['extension'] = cmds.getAttr('defaultRenderGlobals.imfPluginKey')
    scene_info['padding'] = int(cmds.getAttr('defaultRenderGlobals.extensionPadding'))
  scene_info['extension'] = scene_info['extension'][:3]

  # collect a dict of attrs that define how output frames have frame numbers
  # and extension added to their names.
  if renderer == 'arnold':
    print '--> output name format'
    scene_info['output_name_format'] = {}
    attr_list = {
      'outFormatControl',
      'animation',
      'putFrameBeforeExt',
      'periodInExt',
      'extensionPadding',
    }
    for name_attr in attr_list:
      if cmds.attributeQuery(name_attr, n='defaultRenderGlobals', ex=True):
        scene_info['output_name_format'][name_attr] = cmds.getAttr('defaultRenderGlobals.%s' % name_attr)

  print '--> output file prefixes'
  prefix = get_layer_override('defaultRenderLayer', renderer, 'prefix')
  scene_info['file_prefix'] = [prefix]
  prefixes_to_verify = [prefix]
  layer_prefixes = {}
  for layer in selected_layers:
    layer_prefix = get_layer_override(layer, renderer, 'prefix')
    if layer_prefix != None:
      layer_prefixes[layer] = layer_prefix
      prefixes_to_verify.append(layer_prefix)
  scene_info['file_prefix'].append(layer_prefixes)

  print '--> files'
  assets = set()
  for asset in get_scene_files(frames_to_render, renderer):
    if asset:
      assets.add(_clean_path(asset))
  for asset in extra_assets:
    if asset:
      assets.add(_clean_path(asset))
  scene_info['files'] = [_clean_path(_absolutize_path(path)) for path in assets]
  # Xgen files are already included in the main files list, but we also
  # include them separately so Zync can perform Xgen-related tasks on
  # the much smaller subset
  scene_info['xgen_files'] = list(set(get_xgen_files()))

  print '--> plugins'
  scene_info['plugins'] = []
  plugin_list = cmds.pluginInfo(query=True, pluginsInUse=True)
  for i in range(0, len(plugin_list), 2):
    scene_info['plugins'].append(str(plugin_list[i]))

  # detect MentalCore
  if renderer == 'mr':
    mentalcore_used = False
    try:
      mc_nodes = cmds.ls(type='core_globals')
      if len(mc_nodes) == 0:
        mentalcore_used = False
      else:
        mc_node = mc_nodes[0]
        if cmds.getAttr('%s.ec' % (mc_node,)) == True:
          mentalcore_used = True
        else:
          mentalcore_used = False
    except:
      mentalcore_used = False
  else:
    mentalcore_used = False
  if mentalcore_used:
    scene_info['plugins'].append('mentalcore')

  # detect use of cache files
  if len(cmds.ls(type='cacheFile')) > 0:
    scene_info['plugins'].append('cache')

  print '--> maya version'
  scene_info['version'] = get_maya_version()

  scene_info['vray_version'] = ''
  if renderer == 'vray':
    print '--> vray version'
    try:
      scene_info['vray_version'] = '.'.join(str(cmds.vray('version')).split('.')[0:3])
      scene_info['vray_production_engine_name'] = _get_vray_production_engine_name()
    except Exception as e:
      raise maya_common.MayaZyncException(_plugin_load_error_message('VRay'))

  scene_info['arnold_version'] = ''
  if renderer == 'arnold':
    print '--> arnold version'
    try:
      scene_info['arnold_version'] = str(cmds.pluginInfo('mtoa', query=True, version=True))
    except Exception as e:
      raise maya_common.MayaZyncException(_plugin_load_error_message('Arnold'))

  if renderer == 'renderman':
    print '--> renderman version'
    try:
      scene_info['renderman_version'] = renderman.get_version()
    except Exception as e:
      raise maya_common.MayaZyncException(_plugin_load_error_message('Renderman'))

  if renderer == 'redshift':
    try:
      scene_info['redshift_version'] = get_redshift_version()
    except Exception as e:
      raise maya_common.MayaZyncException(_plugin_load_error_message('Redshift'))

  if renderer == 'arnold':
    _check_arnold_gpu()

    # If this is an Arnold job and AOVs are on, include a list of AOV
    # names in scene_info. If "Merge AOVs" is on, i.e. multichannel EXRs,
    # the AOVs will be rendered in a single image, so consider AOVs to be
    # OFF for purposes of the Zync job.
    try:
      aov_on = (cmds.getAttr('defaultArnoldRenderOptions.aovMode') and
        not cmds.getAttr('defaultArnoldDriver.mergeAOVs'))
      override_prefix = cmds.getAttr('defaultArnoldDriver.prefix')
    except:
      aov_on = False
      override_prefix = ''
    should_override_prefix = bool(override_prefix)
    if aov_on:
      print '--> AOVs'
      scene_info['aovs'] = [cmds.getAttr('%s.name' % (n,)) for n in cmds.ls(type='aiAOV')]

      if scene_info['aovs']:
        # Here goes verification of the output prefixes. Once the AOVs are about
        # to render into the separate files, output prefix is suppose to contain
        # <RenderPass> tag. The regular prefixes can be override by the one set
        # up in the defaultArnoldDriver
        output_prefix_aov_warning = False
        for output in prefixes_to_verify:
          if not output or '<RenderPass>' not in output:
            output_prefix_aov_warning = True

        SubmissionCheck(
            check=lambda: (output_prefix_aov_warning and not should_override_prefix) or \
                          (should_override_prefix and '<RenderPass>' not in override_prefix),
            title='RenderPass tag missing',
            message='AOVs are selected to render into separate files, but the '
                    'output prefix of one of the layers does not contain '
                    '<RenderPass> tag. Are you sure the configuration is correct?',
        ).run_check()

    else:
      scene_info['aovs'] = []

  if renderer == 'vray':
    print '--> bake GI flag'
    scene_info['bake_gi'] = False;
    try:
      if cmds.getAttr('vraySettings.gi'):
        primary_engine = int(cmds.getAttr('vraySettings.pe'))
        secondary_engine = int(cmds.getAttr('vraySettings.se'))
        _NONE_RENDERER_ID = 0
        _BRUTE_FORCE_RENDERER_ID = 2
        scene_info['bake_gi'] = primary_engine != _BRUTE_FORCE_RENDERER_ID or \
                                (secondary_engine != _NONE_RENDERER_ID and secondary_engine != _BRUTE_FORCE_RENDERER_ID)
    except:
      pass

  # collect info on whether scene uses Legacy Render Layers or new Render
  # Setup system (Maya 2016.5 and higher only)
  if float(get_maya_version()) >= 2016.5:
    print '--> renderSetupEnable'
    # 0 or 1. 0 = legacy render layers, 1 = new render setup system
    if cmds.optionVar(exists='renderSetupEnable'):
      scene_info['renderSetupEnable'] = cmds.optionVar(query='renderSetupEnable')
    else:
      scene_info['renderSetupEnable'] = 1

  return scene_info


def _clean_path(path):
  return path.replace('\\', '/')


def _plugin_load_error_message(plugin_name):
  message = 'Could not detect {0} version. ' \
            'This is required to render {0} jobs. ' \
            'Do you have the {0} plugin loaded?'
  return message.format(plugin_name)


def _check_arnold_gpu():
  try:
    renderDevice = cmds.getAttr('defaultArnoldRenderOptions.renderDevice')
  except:
    renderDevice = 0

  SubmissionCheck(
      check=lambda: (renderDevice != 0 and renderDevice != '0'),
      title='GPU rendering not supported',
      always_fail=True,
      message='Zync does not currently support GPU rendering for Arnold. '
              'Please change render device to CPU in render settings.',
  ).run_check()


def _absolutize_path(path):
  if path:
    # Maya sometimes prefixes relative paths with //, but abspath considers
    # such paths as absolute, so // needs to be removed
    if path.startswith('//'):
      path = path.replace('//', '', 1)
    if not os.path.isabs(path):
      return os.path.abspath(os.path.join(proj_dir(), path))
  return path


def _get_bake_set_uvs(bake_set):
  conn_list = cmds.listConnections(bake_set)
  if conn_list == None or len(conn_list) == 0:
    return None
  return cmds.polyEvaluate(conn_list[0], b2=True)


def _get_bake_set_map(bake_set):
  return cmds.getAttr('%s.bakeChannel' % bake_set)


def _get_bake_set_shape(bake_set):
  transforms = cmds.listConnections(bake_set)
  if transforms == None or len(transforms) == 0:
    return None
  transform = transforms[0]
  shape_nodes = cmds.listRelatives(transform)
  if shape_nodes == None or len(shape_nodes) == 0:
    return None
  return shape_nodes[0]


def _get_bake_set_output_path(bake_set):
  out_path = cmds.getAttr('%s.outputTexturePath' % bake_set)
  out_path = out_path.replace('\\', '/')
  if out_path[0] == '/' or out_path[1] == ':':
    full_path = out_path
  else:
    full_path = proj_dir().replace('\\', '/')
    if full_path[-1] != '/':
      full_path += '/'
    full_path += out_path
  return full_path


def _get_vray_production_engine_name():
  """Get the vray production engine if the renderer is set to vray.

  Return:
    str, see VRAY_ENGINE_NAME_xxx constants for possible values
  """
  try:
    engine_id = cmds.getAttr('vraySettings.productionEngine')
    if engine_id == 0:
      return VRAY_ENGINE_NAME_CPU
    elif engine_id == 1:
      return VRAY_ENGINE_NAME_OPENCL
    elif engine_id == 2:
      return VRAY_ENGINE_NAME_CUDA
    return VRAY_ENGINE_NAME_UNKNOWN
  except ValueError:
    return VRAY_ENGINE_NAME_CPU


def _switch_to_renderlayer(layer_name):
  # Use the newer Render Setup API if it exists and Render Setup is enabled.
  if (_RENDERSETUP_IMPORT_ERROR is None and
      cmds.optionVar(exists='renderSetupEnable') and
      cmds.optionVar(query='renderSetupEnable') and
      not os.getenv('MAYA_ENABLE_LEGACY_RENDER_LAYERS')):
    rs = renderSetup.instance()
    # defaultRenderLayer doesn't exist as a Render Setup layer, it must be
    # treated as a legacy layer always.
    if layer_name == 'defaultRenderLayer':
      rs.switchToLayerUsingLegacyName('defaultRenderLayer')
    else:
      # The rest of the zync-maya code works with legacy render layer names,
      # which prefix Render Setup layers with an rs_ prefix.
      if layer_name.startswith('rs_'):
        layer_name = layer_name[3:]
      rs.switchToLayer(rs.getRenderLayer(layer_name))
  else:
    cmds.editRenderLayerGlobals(currentRenderLayer=layer_name)


def _maya_attr_is_true(attr_val):
  """Whether a Maya attr evaluates to True.

  When querying an attribute value from an ambiguous object the Maya API will return
  a list of values, which need to be properly handled to evaluate properly.
  """
  if isinstance(attr_val, types.BooleanType):
    return attr_val
  elif isinstance(attr_val, (types.ListType, types.GeneratorType)):
    return any(attr_val)
  else:
    return bool(attr_val)


def _unused(*args):
  """Method to mark a variable as unused.

  Args:
    *args: does nothing
  """
  _ = args
  pass


# TODO(cipriano) Move this function into zync-python. (b/79435050)
def parse_frame_range(frame_range):
  frame_list = list()
  for frange_section in frame_range.split(','):
    frame_list.extend(_parse_frame_range_section(frange_section))
  return frame_list


# TODO(cipriano) Support embedded step number. (b/70778535)
def _parse_frame_range_section(frange_section):
  range_match = _FRAME_RANGE_RE.match(frange_section)
  if range_match:
    start_frame = int(range_match.group('sf'))
    end_frame = int(range_match.group('ef'))
    if end_frame >= start_frame:
      return range(start_frame, end_frame+1)
    else:
      return range(start_frame, end_frame-1, -1)

  single_frame_match = _SINGLE_FRAME_RE.match(frange_section)
  if single_frame_match:
    return [int(single_frame_match.group(0))]

  raise ValueError('unable to parse frame range section %s' % frange_section)


# TODO(cipriano) Move this function into zync-python. (b/79435050)
def extract_frame_number_from_file_path(file_path):
  frame_match = _FRAME_NUMBER_RE.match(os.path.basename(file_path))
  if frame_match:
    return int(frame_match.group('frame'))
  return None


class SubmissionCheck(object):
  """
  Manages the running of submission checks and display of confirmation dialogs.
  """
  def __init__(self, check,  title, message='', check_args=None, check_kwargs=None,
               confirm_msg='Yes, submit job.', cancel_msg='No, cancel job submission.',
               always_fail=False):
    """
    Initialize Check and set attributes

    Args:
      check: callable, function that runs check, callable should return a bool.
      title: str, title of confirm dialog window.
      message: str, message to display on confirm dialog.
      check_args: list, list of arguments to pass to check function.
      check_kwargs: dict, keyword arguments to pass to check function.
      confirm_msg: str, text to display on confirm button.
      cancel_msg: str, text to display on cancel button.
    """
    self.title = title
    self.message = message
    self.check = check
    self.check_args = check_args if check_args is not None else []
    self.check_kwargs = check_kwargs if check_kwargs is not None else {}
    self.confirm_msg = confirm_msg
    self.cancel_msg = cancel_msg
    self.always_fail = always_fail

  def confirm_or_abort(self):
    """
    Display a confirmation dialog, allowing the user to continue or abort the job submission

    Raises:
      ZyncAbortedByUser exception if user aborts.
    """
    response = cmds.confirmDialog(
      title=self.title,
      message=self.message,
      button=(self.confirm_msg, self.cancel_msg),
      defaultButton=self.confirm_msg,
      cancelButton=self.cancel_msg,
      icon='warning')
    if response != self.confirm_msg:
      raise maya_common.ZyncAbortedByUser('Aborted by user')

  def run_check(self, show_confirmation=True):
    """
    Run the check and show the confirmation dialog.
    Args:
      show_confirmation: bool, whether or not to show the confirmation dialog.

    Returns:
      bool, the return value of the check.

    Raises:
      ZyncSubmissionCheckError when there is an error running the submission check or if check does not return a bool.
    """
    try:
      check_return = self.check(*self.check_args, **self.check_kwargs)
    except Exception, e:
      raise maya_common.ZyncSubmissionCheckError('{}: {}'.format(self.title, e.message))
    if not isinstance(check_return, bool):
      raise maya_common.ZyncSubmissionCheckError('{}: Invalid check. Did not return a boolean.'.format(self.title))
    if check_return:
      if self.always_fail:
        raise maya_common.ZyncSubmissionCheckError(self.message)
      elif show_confirmation:
        self.confirm_or_abort()
    return check_return


class SubmitWindow(object):
  """
  A Maya UI window for submitting to Zync
  """
  @show_exceptions
  def __init__(self, title='Zync Submit (version %s)' % __version__):
    """
    Constructs the window.
    You must call show() to display the window.
    """
    import_zync_python()
    self.title = title

    scene_name = cmds.file(q=True, loc=True)
    if scene_name == 'unknown':
      raise maya_common.MayaZyncException('Please save your script before launching a job.')

    # this will perform the Google OAuth flow so future API requests
    # will be authenticated
    self.zync_conn = zync.Zync(application='maya')

    self.vray_production_engine_name = VRAY_ENGINE_NAME_UNKNOWN

    self.new_project_name = self.zync_conn.get_project_name(scene_name)

    self.num_instances = 1
    self.priority = 50
    self.parent_id = None

    self.project = proj_dir()
    if self.project[-1] == '/':
      self.project = self.project[:-1]

    self.frange = frame_range()
    self.udim_range = udim_range()
    self.frame_step = cmds.getAttr('defaultRenderGlobals.byFrameStep')
    self.chunk_size = 10
    self.upload_only = 0
    self.start_new_slots = 1
    self.skip_check = 0
    self.notify_complete = 0
    self.vray_nightly = 0
    self.use_standalone = 0
    self.ignore_second_deps = 0
    self.num_tiles = 1
    self.ignore_plugin_errors = 0
    self.login_type = 'zync'

    mi_setting = self.zync_conn.CONFIG.get('USE_MI')
    if mi_setting in (None, '', 1, '1'):
      self.force_mi = True
    else:
      self.force_mi = False

    self.x_res = cmds.getAttr('defaultResolution.width')
    self.y_res = cmds.getAttr('defaultResolution.height')

    self.parse_renderer_from_scene()
    self.init_layers()
    self.init_bake()
    self.init_tiled_rendering()

    self.name = self.loadUI(UI_FILE)

    self.check_references()

  def loadUI(self, ui_file):
    """
    Loads the UI and does post-load commands.
    """
    # Maya 2016 and up will use Maya IO by default.
    self.is_maya_io = (float(get_maya_version()) >= 2016)
    # Create some new functions. These functions are called by UI elements in
    # resources/submit_dialog.ui. Each UI element in that file uses these
    # functions to query this window Object for its initial value.
    #
    # For example, the "frange" textbox calls cmds.submit_callb('frange'),
    # which causes its value to be set to whatever the value of self.frange
    # is currently set to.
    #
    # Initial values can also be function based. For example, the "renderer"
    # dropdown calls cmds.submit_callb('renderer'), which in turn triggers
    # self.init_renderer().
    #
    # The UI doesn't have a reference to this window Object, but it does have
    # access to the Maya API. So we monkey patch these new functions into the
    # API so the UI can in effect call class functions.
    cmds.submit_callb = self.get_initial_value
    cmds.do_submit_callb = self.submit
    cmds.select_files_callb = self.select_files
    cmds.login_with_google_callb = self.login_with_google
    cmds.logout_callb = self.logout

    #
    #  Delete the "ZyncSubmitDialog" window if it exists.
    #
    if cmds.window('ZyncSubmitDialog', q=True, ex=True):
      cmds.deleteUI('ZyncSubmitDialog')

    #
    #  Load the UI file. See the init_* functions below for more info on
    #  what each UI element does as it's loaded.
    #
    name = cmds.loadUI(f=ui_file)

    # If topLeftCorner is unspecified, Maya 2019 puts the dialog in the top-left corner
    # of the screen in a way that makes title bar invisible
    cmds.window(name, e=True, title=self.title, topLeftCorner=(50, 50))

    #
    #  Callbacks - set up functions to be called as UI elements are modified.
    #
    cmds.textField('num_instances', e=True, changeCommand=self.change_num_instances)
    cmds.optionMenu('instance_type', e=True, changeCommand=self.change_instance_type)
    cmds.radioButton('existing_project', e=True, onCommand=self.select_existing_project)
    cmds.radioButton('new_project', e=True, onCommand=self.select_new_project)
    cmds.checkBox('upload_only', e=True, changeCommand=self.upload_only_toggle)
    cmds.optionMenu('renderer', e=True, changeCommand=self.change_renderer)
    cmds.optionMenu('job_type', e=True, changeCommand=self.change_job_type)
    if self.tiled_rendering_enabled:
      cmds.textField('num_tiles', e=True, changeCommand=self.change_num_tiles)
    else:
      cmds.textField('num_tiles', e=True, vis=False)
      cmds.text('label_num_tiles', e=True, vis=False)
    cmds.checkBox('sync_extra_assets', e=True, changeCommand=self.sync_extra_assets_toggle)
    cmds.button('select_files', e=True, enable=False)
    cmds.textScrollList('layers', e=True, selectCommand=self.change_layers)
    # No point in even showing the standalone option to users of old Maya, where
    # we force standalone use.
    cmds.checkBox('use_standalone', e=True, changeCommand=self.change_standalone,
                  vis=self.is_maya_io)

    #
    #  Call a few of those callbacks now to set initial UI state.
    #
    self.change_renderer(self.renderer)
    self.select_new_project(True)
    self.set_user_label(self.zync_conn.email)

    return name

  @show_exceptions
  def upload_only_toggle(self, checked):
    if checked:
      cmds.textField('num_instances', e=True, en=False)
      cmds.optionMenu('instance_type', e=True, en=False)
      cmds.checkBox('skip_check', e=True, en=False)
      cmds.textField('output_dir', e=True, en=False)
      cmds.optionMenu('renderer', e=True, en=False)
      cmds.optionMenu('job_type', e=True, en=False)
      cmds.checkBox('vray_nightly', e=True, en=False)
      cmds.checkBox('use_standalone', e=True, en=False)
      cmds.textField('frange', e=True, en=False)
      cmds.textField('frame_step', e=True, en=False)
      cmds.textField('chunk_size', e=True, en=False)
      cmds.optionMenu('camera', e=True, en=False)
      cmds.textScrollList('layers', e=True, en=False)
      cmds.textField('x_res', e=True, en=False)
      cmds.textField('y_res', e=True, en=False)
    else:
      cmds.textField('num_instances', e=True, en=True)
      cmds.optionMenu('instance_type', e=True, en=True)
      cmds.checkBox('skip_check', e=True, en=True)
      cmds.textField('output_dir', e=True, en=True)
      cmds.optionMenu('renderer', e=True, en=True)
      cmds.optionMenu('job_type', e=True, en=True)
      cmds.textField('frange', e=True, en=True)
      cmds.textField('frame_step', e=True, en=True)
      cmds.textField('chunk_size', e=True, en=True, changeCommand=self.change_chunk_size)
      cmds.optionMenu('camera', e=True, en=True)
      cmds.textScrollList('layers', e=True, en=True)
      cmds.textField('x_res', e=True, en=True)
      cmds.textField('y_res', e=True, en=True)
      self.change_renderer(eval_ui('renderer', ui_type='optionMenu', v=True))

  @show_exceptions
  def sync_extra_assets_toggle(self, checked):
    """Event triggered when the Sync Extra Assets control is toggled.

    Args:
      checked: bool, whether the checkbox is checked
    """
    cmds.button('select_files', e=True, enable=checked)

  @show_exceptions
  def change_num_instances(self, *args, **kwargs):
    _unused(args)
    _unused(kwargs)
    self.update_est_cost()

  @show_exceptions
  def change_num_tiles(self, num_tiles):
    if int(num_tiles) > 1:
      cmds.textField('chunk_size', e=True, tx='1')

  @show_exceptions
  def change_chunk_size(self, chunk_size):
    if int(chunk_size) > 1 and self.tiled_rendering_enabled:
      cmds.textField('num_tiles', e=True, tx='1')

  @show_exceptions
  def change_instance_type(self, *args, **kwargs):
    _unused(args)
    _unused(kwargs)
    self.update_est_cost()

  @show_exceptions
  def change_renderer(self, renderer):
    cmds.checkBox('vray_nightly', e=True, en=False)
    cmds.checkBox('vray_nightly', e=True, vis=False)
    cmds.checkBox('vray_nightly', e=True, v=False)
    if renderer in ('vray', 'V-Ray'):
      renderer_key = 'vray'
      cmds.checkBox('use_standalone', e=True, en=True)
      cmds.checkBox('use_standalone', e=True, v=False)
      cmds.checkBox('use_standalone', e=True, label='Use Vray Standalone')
      cmds.checkBox('use_standalone', e=True, vis=True)
      cmds.checkBox('ignore_second_deps', e=True, vis=False)
      cmds.textField('num_tiles', e=True, en=False)
    elif renderer.lower() == 'arnold':
      renderer_key = 'arnold'
      cmds.checkBox('use_standalone', e=True, en=True)
      cmds.checkBox('use_standalone', e=True, v=False)
      cmds.checkBox('use_standalone', e=True, label='Use Arnold Standalone')
      cmds.checkBox('use_standalone', e=True, vis=True)
      cmds.checkBox('ignore_second_deps', e=True, vis=False)
      cmds.textField('num_tiles', e=True, en=False)
    elif renderer.lower() == 'renderman':
      renderer_key = 'renderman'
      cmds.checkBox('use_standalone', e=True, v=False)
      cmds.checkBox('use_standalone', e=True, en=False)
      cmds.checkBox('use_standalone', e=True, label='Use Standalone')
      cmds.checkBox('use_standalone', e=True, vis=True)
      cmds.checkBox('ignore_second_deps', e=True, vis=True)
      cmds.textField('num_tiles', e=True, en=False)
      cmds.textField('num_tiles', e=True, tx='1')
    elif renderer.lower() == 'redshift':
      renderer_key = 'redshift'
      cmds.checkBox('use_standalone', e=True, vis=False)
      cmds.checkBox('use_standalone', e=True, en=False)
      cmds.checkBox('use_standalone', e=True, v=False)
      cmds.checkBox('ignore_second_deps', e=True, vis=True)
      cmds.textField('num_tiles', e=True, en=False)
      cmds.textField('num_tiles', e=True, tx='1')
    else:
      raise maya_common.MayaZyncException('Unrecognized renderer "%s".' % renderer)
    cmds.textField('chunk_size', e=True, en=True, changeCommand=self.change_chunk_size)

    #  job_types dropdown - remove all items for list, then allow in job types
    #  from self.zync_conn.JOB_SUBTYPES
    old_types = cmds.optionMenu('job_type', q=True, ill=True)
    if old_types != None:
      cmds.deleteUI(old_types)
    first_type = None
    visible = False
    if renderer_key != None and renderer_key in self.job_types:
      for job_type in self.job_types[renderer_key]:
        if first_type == None:
          first_type = job_type
        label = string.capwords(job_type)
        if label != 'Render':
          visible = True
        print cmds.menuItem(parent='job_type', label=label)
    else:
      print cmds.menuItem(parent='job_type', label='Render')
      first_type = 'Render'
    cmds.optionMenu('job_type', e=True, vis=visible)
    cmds.text('job_type_label', e=True, vis=visible)
    self.change_job_type(first_type)
    # force refresh of a few other UI elements
    self.init_instance_type()
    self.update_est_cost()
    self.change_standalone(eval_ui('use_standalone', 'checkBox', v=True))
    self.init_output_dir()

  @show_exceptions
  def change_job_type(self, job_type):
    job_type = job_type.lower()
    if job_type == 'render':
      cmds.textField('output_dir', e=True, en=True)
      cmds.text('frange_label', e=True, label='Frame Range:')
      cmds.textField('frange', e=True, tx=self.frange)
      cmds.optionMenu('camera', e=True, en=True)
      cmds.text('layers_label', e=True, label='Render Layers:')
      cmds.textScrollList('layers', e=True, removeAll=True)
      cmds.textScrollList('layers', e=True, append=self.layers)
      if len(self.layers) == 1:
        cmds.textScrollList('layers', e=True, selectIndexedItem=1)
      cmds.textField('x_res', e=True, tx=self.x_res)
      cmds.textField('y_res', e=True, tx=self.y_res)
    elif job_type == 'bake':
      cmds.textField('output_dir', e=True, en=False)
      cmds.text('frange_label', e=True, label='UDIM Range:')
      cmds.textField('frange', e=True, tx=self.udim_range)
      cmds.optionMenu('camera', e=True, en=False)
      cmds.text('layers_label', e=True, label='Bake Sets:')
      cmds.textScrollList('layers', e=True, removeAll=True)
      cmds.textScrollList('layers', e=True, append=self.bake_sets)
      try:
        default_x_res = str(cmds.getAttr('vrayDefaultBakeOptions.resolutionX'))
      except:
        default_x_res = ''
      cmds.textField('x_res', e=True, tx=default_x_res)
      try:
        default_y_res = str(cmds.getAttr('vrayDefaultBakeOptions.resolutionY'))
      except:
        default_y_res = ''
      cmds.textField('y_res', e=True, tx=default_y_res)
    else:
      cmds.error('Unknown Job Type "%s".' % (job_type,))

  @show_exceptions
  def change_layers(self):
    if cmds.optionMenu('job_type', q=True, v=True).lower() != 'bake':
      return
    if cmds.textScrollList('layers', q=True, nsi=True) > 1:
      return
    bake_sets = eval_ui('layers', 'textScrollList', ai=True, si=True)
    bake_set = bake_sets[0]
    cmds.textField('x_res', e=True, tx=cmds.getAttr('%s.resolutionX' % (bake_set,)))
    cmds.textField('y_res', e=True, tx=cmds.getAttr('%s.resolutionY' % (bake_set,)))

  @show_exceptions
  def change_standalone(self, checked):
    """Event triggered when the Use Standalone control is toggled.

    Args:
      checked: bool, whether the checkbox is checked
    """
    current_renderer = self.get_renderer()
    # if using arnold standalone, disable chunk size. arnold stores info
    # one-frame-per-file so chunk size is not applicable.
    if current_renderer == 'arnold' and checked:
      cmds.textField('chunk_size', e=True, en=False)
    else:
      cmds.textField('chunk_size', e=True, en=True)

    if (current_renderer == 'vray' or current_renderer == 'arnold') and checked:
      cmds.textField('num_tiles', e=True, en=True)
    else:
      cmds.textField('num_tiles', e=True, en=False)

  @show_exceptions
  def select_new_project(self, selected):
    if selected:
      cmds.textField('new_project_name', e=True, en=True)
      cmds.optionMenu('existing_project_name', e=True, en=False)

  @show_exceptions
  def select_existing_project(self, selected):
    if selected:
      cmds.textField('new_project_name', e=True, en=False)
      cmds.optionMenu('existing_project_name', e=True, en=True)

  def check_references(self):
    """
    Run any checks to ensure all reference files are accurate. If not,
    raise an Exception to halt the submit process.

    This function currently does nothing. Before Maya Binary was supported
    it checked to ensure no .mb files were being used.
    """
    #for ref in cmds.file(q=True, r=True):
    #   if check_failed:
    #     raise Exception(msg)
    pass

  def get_render_params(self):
    """
    Returns a dict of all the render parameters set on the UI
    """
    params = dict()

    if cmds.radioButton('existing_project', q=True, sl=True) == True:
      proj_name = eval_ui('existing_project_name', 'optionMenu', v=True)
      if proj_name == None or proj_name.strip() == '':
        raise maya_common.MayaZyncException('Your project name cannot be blank. Please '
                                'select New Project and enter a name.')
    else:
      proj_name = eval_ui('new_project_name', text=True)
    params['proj_name'] = proj_name

    parent = eval_ui('parent_id', text=True).strip()
    if parent != None and parent != '':
      params['parent_id'] = parent
    params['upload_only'] = int(eval_ui('upload_only', 'checkBox', v=True))
    params['start_new_slots'] = self.start_new_slots
    params['skip_check'] = int(eval_ui('skip_check', 'checkBox', v=True))
    params['notify_complete'] = int(eval_ui('notify_complete', 'checkBox', v=True))
    params['project'] = eval_ui('project', text=True)
    params['sync_extra_assets'] = int(eval_ui('sync_extra_assets', 'checkBox', v=True))

    #
    # Get the output path. If it is a relative path, convert it to an
    # absolute path by joining it to the Maya project path.
    #
    params['out_path'] = eval_ui('output_dir', text=True)
    if not os.path.isabs(params['out_path']):
      params['out_path'] = os.path.abspath(os.path.join(params['project'],
        params['out_path']))

    params['ignore_plugin_errors'] = int(eval_ui('ignore_plugin_errors', 'checkBox', v=True))

    params['renderer'] = self.get_renderer()

    params['job_subtype'] = eval_ui('job_type', ui_type='optionMenu', v=True).lower()

    params['priority'] = int(eval_ui('priority', text=True))
    params['num_instances'] = int(eval_ui('num_instances', text=True))
    params['num_tiles'] = int(eval_ui('num_tiles', text=True))

    selected_type = self.zync_conn.machine_type_from_label(
        eval_ui('instance_type', 'optionMenu', v=True), params['renderer'] + '-maya')
    if not selected_type:
      raise maya_common.MayaZyncException('Unknown instance type selected: %s' % selected_type)
    params['instance_type'] = selected_type

    params['frange'] = eval_ui('frange', text=True)
    params['step'] = self._get_frame_step_param()
    params['chunk_size'] = int(eval_ui('chunk_size', text=True))
    params['xres'] = int(eval_ui('x_res', text=True))
    params['yres'] = int(eval_ui('y_res', text=True))
    params['use_standalone'] = 0

    params['camera'] = eval_ui('camera', 'optionMenu', v=True)
    if not params['camera']:
      raise maya_common.MayaZyncException('Please select a render camera. If the list is '
                              'empty, try adding a renderable camera in your '
                              'scene render settings.')

    if params['upload_only'] == 0 and params['renderer'] == 'vray':
      params['vray_nightly'] = int(eval_ui('vray_nightly', 'checkBox', v=True))
      if params['use_standalone'] == 1 and params['job_subtype'] == 'bake':
        cmds.error('Vray Standalone is not currently supported for Bake jobs.')
    elif params['upload_only'] == 0 and params['renderer'] == 'mr':
      params['vray_nightly'] = 0
    elif params['upload_only'] == 0 and params['renderer'] == 'arnold':
      params['vray_nightly'] = 0
    else:
      params['vray_nightly'] = 0

    if params['upload_only'] == 1:
      params['layers'] = None
      params['bake_sets'] = None
    elif params['job_subtype'] == 'bake':
      bake_sets = eval_ui('layers', 'textScrollList', ai=True, si=True)
      if not bake_sets:
        raise maya_common.MayaZyncException('Please select bake set(s).')
      bake_sets = ','.join(bake_sets)
      params['bake_sets'] = bake_sets
      params['layers'] = None
    else:
      layers = eval_ui('layers', 'textScrollList', ai=True, si=True)
      if not layers:
        raise maya_common.MayaZyncException('Please select layer(s) to render.')
      layers = ','.join(layers)
      params['layers'] = layers
      params['bake_sets'] = None

    return params

  def _get_frame_step_param(self):
    try:
      step = int(eval_ui('frame_step', text=True))
      if step < 1:
        raise ValueError
      return step
    except ValueError:
      raise maya_common.MayaZyncException('Zync only supports whole numbers >=1 for Frame Step.')

  @show_exceptions
  def show(self):
    """
    Displays the window.
    """
    cmds.showWindow(self.name)

  def init_bake(self):
    self.bake_sets = (bake_set for bake_set in cmds.ls(type='VRayBakeOptions') \
      if bake_set != 'vrayDefaultBakeOptions')
    self.bake_sets = list(self.bake_sets)
    self.bake_sets.sort()

  def init_tiled_rendering(self):
    self.tiled_rendering_enabled = self.zync_conn.is_experiment_enabled("EXPERIMENT_TILED_RENDERING")

  #
  #  These init_* functions get run automatcially when the UI file is loaded.
  #  The function names must match the name of the UI element e.g. init_camera()
  #  will be run when the "camera" UI element is initialized.
  #

  def init_layers(self):
    self.layers = get_render_layers()

  def init_existing_project_name(self):
    self.projects = self.zync_conn.get_project_list()
    project_found = False
    for project in self.projects:
      cmds.menuItem(parent='existing_project_name', label=project['name'])
      if project['name'] == self.new_project_name:
        project_found = True
    if project_found:
      cmds.optionMenu('existing_project_name', e=True, v=self.new_project_name)
    if len(self.projects) == 0:
      cmds.radioButton('existing_project', e=True, en=False)
    else:
      cmds.radioButton('existing_project', e=True, en=True)

  def init_instance_type(self):
    old_selection = eval_ui('instance_type', ui_type='optionMenu', v=True)
    old_types = cmds.optionMenu('instance_type', q=True, ill=True)
    if old_types is not None:
      cmds.deleteUI(old_types)
    current_renderer = '%s-maya' % self.get_renderer()
    set_to = None

    self.refresh_instance_types_cache()
    for label in self.zync_conn.get_machine_type_labels(current_renderer):
      if label == old_selection:
        set_to = label
      cmds.menuItem(parent='instance_type', label=label)
    if set_to:
      cmds.optionMenu('instance_type', e=True, v=set_to)
    self.update_est_cost()

  def refresh_instance_types_cache(self):
    usage_tag = None
    if self.renderer == 'vray':
      if self.vray_production_engine_name == VRAY_ENGINE_NAME_CUDA:
        usage_tag = 'maya_vray_rt'
    elif self.renderer == 'redshift':
      usage_tag = 'maya_redshift'
    self.zync_conn.refresh_instance_types_cache(renderer=self.renderer, usage_tag=usage_tag)

  def parse_renderer_from_scene(self):
    # Try to detect the currently selected renderer, so it will be selected
    # when the form appears. If we can't, fall back to the default set in zync.py.
    current_renderer = cmds.getAttr('defaultRenderGlobals.currentRenderer')
    if current_renderer == 'mentalRay':
      key = 'mr'
    elif current_renderer == 'vray':
      key = 'vray'
    elif current_renderer == 'arnold':
      key = 'arnold'
    # handle 'renderman', renderMan' and 'renderManRIS'
    elif current_renderer.lower().startswith('renderman'):
      key = 'renderman'
    elif current_renderer == 'redshift':
      key = 'redshift'
    else:
      key = 'vray'
    # if that renderer is not supported, default to Vray
    self.renderer = key

    # read vray production engine
    if key == 'vray':
      self.vray_production_engine_name = _get_vray_production_engine_name()

  def init_renderer(self):
    #  Add the list of renderers to UI element.
    rend_found = False
    default_renderer_name = RENDERER_NAMES.get(self.renderer, 'vray')

    if self.vray_production_engine_name == VRAY_ENGINE_NAME_CUDA:
      cmds.menuItem(parent='renderer', label=RENDER_LABEL_VRAY_CUDA)
      cmds.optionMenu('renderer', e=True, v=RENDER_LABEL_VRAY_CUDA, enable=False)
    else:
      for item in RENDERER_NAMES.values():
        cmds.menuItem(parent='renderer', label=item)
        if item == default_renderer_name:
          rend_found = True
      if rend_found:
        cmds.optionMenu('renderer', e=True, v=default_renderer_name)

  def init_job_type(self):
    self.job_types = self.zync_conn.JOB_SUBTYPES['maya']

  def init_camera(self):
    cam_parents = [cmds.listRelatives(x, ap=True)[-1] for x in cmds.ls(cameras=True)]
    for cam in cam_parents:
      # Only show renderable cameras, but look at render layer overrides to see
      # if cameras are set to renderable in other layers.
      if (_maya_attr_is_true(cmds.getAttr(cam + '.renderable')) or
          any([_maya_attr_is_true(override)
               for override in _get_layer_overrides('%s.renderable' % cam)])):
        cmds.menuItem(parent='camera', label=cam)

  def init_output_dir(self):
    # renderman doesn't use standard project settings, it has its own
    # preference.
    if self.get_renderer() == 'renderman':
      default_output_dir = renderman.get_output_dir()
    else:
      # the project settings define where that project's rendered images should
      # go. get this project setting, defaulting to "images" if it's not found
      # or blank.
      images_rule = cmds.workspace(fileRuleEntry='images')
      if not images_rule or not images_rule.strip():
        images_rule = 'images'
      # this is usually a relative path, and if it is it's relative to the
      # project directory. if image_rule is an absolute path os.path.join
      # will throw out the project dir.
      default_output_dir = os.path.join(cmds.workspace(q=True, rd=True), images_rule)
    cmds.textField('output_dir', e=True, tx=default_output_dir)

  def update_est_cost(self):
    renderer = '%s-maya' % self.get_renderer()
    machine_type = self.zync_conn.machine_type_from_label(
        eval_ui('instance_type', ui_type='optionMenu', v=True), renderer)
    if machine_type and renderer:
      machine_type_price = self.zync_conn.get_machine_type_price(machine_type, renderer)
      if machine_type_price:
        num_machines = int(eval_ui('num_instances', text=True))
        text = '$%.02f' % (num_machines * machine_type_price)
      else:
        text = 'Not Available'
    else:
      text = 'Not Available'
    cmds.text('est_cost', e=True, label='Est. Cost per Hour: %s' % text)

  def get_renderer(self):
    """Get the renderer which is currently selected in the Zync plugin.
    The label shown in the menu (and returned be eval_ui) is slightly
    different than what we want, so we need to translate it based on
    the master list of renderers.

    Returns:
      str, the currently selected renderer, or None if we weren't
      able to identify the one selected.
    """
    selected_renderer_label = eval_ui('renderer', ui_type='optionMenu', v=True)
    for renderer, renderer_label in RENDERER_NAMES.iteritems():
      if renderer_label == selected_renderer_label:
        return renderer
    if selected_renderer_label == RENDER_LABEL_VRAY_CUDA:
      return 'vray'
    return None

  def set_user_label(self, username):
    cmds.text('google_login_status', e=True, label='Logged in as %s' % username)

  def clear_user_label(self):
    cmds.text('google_login_status', e=True, label='')

  @show_exceptions
  def get_initial_value(self, name):
    """Returns the initial value for a given attribute.

    Args:
      name: str the attribute name

    Returns:
      str, the initial attribute value, or "Undefined" if the attribute was
        not found
    """
    init_name = '_'.join(('init', name))
    if hasattr(self, init_name):
      return getattr(self, init_name)()
    elif hasattr(self, name):
      return getattr(self, name)
    else:
      return 'Undefined'

  @show_exceptions
  def login_with_google(self):
    """Perform the Google OAuth flow."""
    self.login_type = 'google'
    self.zync_conn.login_with_google()
    self.set_user_label(self.zync_conn.email)

  @show_exceptions
  def logout(self):
    self.zync_conn.logout()
    self.clear_user_label()

  @show_exceptions
  def select_files(self):
    import_zync_python()
    import file_select_dialog
    proj_name = eval_ui('new_project_name', text=True)
    self.file_select_dialog = file_select_dialog.FileSelectDialog(proj_name)
    self.file_select_dialog.show()

  @show_exceptions
  def _submit_vray_job(self, layer_list, params, sf, ef):
    """Collects info, exports vrscenes and sends jobs

    See also: export_vrscene

    Args:
      layer_list: [str], List of layers names
      params: dict, render job parameters
      start_frame: int, the first frame to export
      end_frame: int, the last frame to export
    """
    print 'Vray job, collecting additional info...'
    self.verify_vray_production_engine()

    print 'Exporting .vrscene files...'
    for layer in layer_list:
      print 'Exporting layer %s...' % layer
      vrscene_path = self.get_standalone_scene_path('vrscene', layer=layer)
      possible_scene_names, render_params = self.export_vrscene(
        vrscene_path, layer, params, sf, ef)

      layer_file = None
      for possible_scene_name in possible_scene_names:
        if os.path.exists(possible_scene_name):
          layer_file = possible_scene_name
          break
      if layer_file is None:
        print 'Failed to find a .vrscene file. Looked for:'
        for item in enumerate(possible_scene_names):
          print "%s: %s" % item
        raise zync.ZyncError(
          'the .vrscene file generated by the Zync Maya plugin '
          'was not found. Unable to submit job.')

      print 'Submitting job for layer %s...' % layer
      self.zync_conn.submit_job('vray', layer_file, params=render_params)

  @show_exceptions
  def _submit_arnold_job(self, layer_list, params, sf, ef):
    """Collects info, exports ass files and sends jobs

    See also: export_ass

    Args:
      layer_list: [str], List of layers names
      params: dict, render job parameters
      start_frame: int, the first frame to export
      end_frame: int, the last frame to export
    """
    print 'Arnold job, collecting additional info...'
    ass_path = self.get_standalone_scene_path('ass')

    print 'Exporting .ass files...'
    for layer in layer_list:
      print 'Exporting layer %s...' % layer
      layer_file_wildcard, render_params = self.export_ass(
          ass_path, layer, params, sf, ef)
      print 'Submitting job for layer %s...' % layer
      self.zync_conn.submit_job(
          'arnold', layer_file_wildcard, params=render_params)

  @show_exceptions
  def submit(self):
    """Submit a job to Zync."""
    if not self.zync_conn.has_user_login():
      raise maya_common.MayaZyncException('You must login before submitting a new job.')

    job_uses_standalone = (not self.is_maya_io or eval_ui('use_standalone', 'checkBox', v=True))

    if not self.verify_eula_acceptance(not job_uses_standalone):
      cmds.error('Job submission canceled.')

    print 'Collecting render parameters...'
    scene_path = cmds.file(q=True, loc=True)
    params = self.get_render_params()

    if 'PREEMPTIBLE' in params['instance_type']:
      import pvm_consent_dialog
      from settings import Settings
      consent_dialog = pvm_consent_dialog.PvmConsentDialog()
      if not Settings.get().get_pvm_ack() and not consent_dialog.prompt():
        return

    if params['sync_extra_assets']:
      import_zync_python()
      import file_select_dialog
      proj_name = eval_ui('new_project_name', text=True)
      extra_assets = file_select_dialog.FileSelectDialog.get_extra_assets(proj_name)
      if not extra_assets:
        raise maya_common.MayaZyncException('No extra assets selected')

    layers_to_render = (params['layers'].split(',') if params['layers'] else None)
    if params['renderer'] == 'renderman':
      renderman.init(layers_to_render, params['camera'])

    submission_checks = [
      SubmissionCheck(
          check=lambda: '(ALPHA)' in params.get('instance_type', ''),
          title='ALPHA instance type selected',
          message='You\'ve selected an instance type for your job which is '
          'still in alpha, and could be unstable for some workloads. '
          'Are you sure you want to submit the job using this '
          'instance type?'),
      SubmissionCheck(
          check=lambda: (cmds.attributeQuery('animation', node='defaultRenderGlobals', exists=True) and
                         not cmds.getAttr('defaultRenderGlobals.animation')),
          title='Animation Off',
          message='It looks like you have animation disabled in your scene. '
          'If you render multiple frames they will probably overwrite '
          'each other. Are you sure you want to submit the job using '
          'these render settings?'),
      SubmissionCheck(
          check=lambda: (cmds.attributeQuery('modifyExtension', node='defaultRenderGlobals', exists=True) and
                         cmds.getAttr('defaultRenderGlobals.modifyExtension')),
          always_fail=True,
          title='Renumber Frames is On',
          message='It looks like you have "Renumber Frames" enabled in your scene. This option is '
          'not supported on Zync and will cause rendered frames to overwrite each other. Please '
          'disable it before continuing.')
    ]

    for submission_check in submission_checks:
      submission_check.run_check()

    print 'Collecting scene info...'
    try:
      params['scene_info'] = get_scene_info(params['renderer'],
          layers_to_render,
          (eval_ui('job_type', ui_type='optionMenu', v=True).lower() == 'bake'),
          extra_assets if params['sync_extra_assets'] else [],
          parse_frame_range(params['frange']))
    except maya_common.ZyncAbortedByUser:
      # If the job is aborted just finish the submit function
      return

    params['plugin_version'] = __version__

    try:
      if job_uses_standalone:
        frange_split = params['frange'].split(',')
        sf = int(frange_split[0].split('-')[0])

        if params['upload_only'] == 1:
          layer_list = ['defaultRenderLayer']
          ef = sf
        else:
          layer_list = params['layers'].split(',')
          ef = int(frange_split[-1].split('-')[-1])

        SubmissionCheck(
            check=output_has_layer_problems,
            title='Layer not in output filename',
            check_args=[params['renderer'], layer_list],
            message='The specified File Name Prefix does not include a layer token (%l, <layer>, <renderlayer>). '
                    'The output rendered files may overwrite each other. Are you sure you want to submit?'
        ).run_check()

        if params['renderer'] == 'vray':
          self._submit_vray_job(layer_list, params, sf, ef)
        elif params['renderer'] == 'arnold':
          self._submit_arnold_job(layer_list, params, sf, ef)
        else:
          raise maya_common.MayaZyncException('Renderer %s unsupported for standalone rendering.' % params['renderer'])

        cmds.confirmDialog(title='Success',
          message='{num_jobs} {label} submitted to Zync.'.format(
            num_jobs=len(layer_list),
            label='job' if len(layer_list) == 1 else 'jobs'),
          button='OK', defaultButton='OK')

      else:
        # Uncomment this section if you want to
        # save a unique copy of the scene file each time your submit a job.
        '''
        original_path = cmds.file(q=True, loc=True)
        original_modified = cmds.file(q=True, modified=True)
        scene_path = generate_scene_path()
        cmds.file(rename=scene_path)
        cmds.file(save=True, type='mayaAscii')
        cmds.file(rename=original_path)
        cmds.file(modified=original_modified)
        '''

        if (cmds.objExists('vraySettings') and
            cmds.attributeQuery('vrscene_on', node='vraySettings', exists=True) and
            cmds.getAttr('vraySettings.vrscene_on')):
          raise maya_common.MayaZyncException('You have "Export to a .vrscene file" turned '
                                  'on. This will cause Vray to attempt a scene '
                                  'export rather than a render. Please disable '
                                  'this option before submitting this scene to '
                                  'Zync for rendering.')

        self.zync_conn.submit_job('maya', scene_path, params=params)
        cmds.confirmDialog(title='Success', message='Job submitted to Zync.',
          button='OK', defaultButton='OK')

    except zync.ZyncPreflightError as e:
      cmds.confirmDialog(title='Preflight Check Failed', message=str(e),
        button='OK', defaultButton='OK')

    except zync.ZyncError as e:
      cmds.confirmDialog(title='Submission Error',
        message='Error submitting job: %s' % (str(e),),
        button='OK', defaultButton='OK', icon='critical')

    else:
      print 'Done.'

  def verify_vray_production_engine(self):
    if self.vray_production_engine_name not in [VRAY_ENGINE_NAME_CPU, VRAY_ENGINE_NAME_CUDA]:
      raise maya_common.MayaZyncException('Current V-Ray production engine is not supported by Zync. '
                              'Please go to Render Settings -> VRay tab to change it to CPU or CUDA')

  @staticmethod
  def export_vrscene(vrscene_path, layer, params, start_frame, end_frame):
    """Export a .vrscene of the current scene.

    Args:
      vrscene_path: str, path to which to export the .vrscene. A layer name will
                    be inserted into the filename.
      layer: str, the name of the render layer to export
      params: dict, render job parameters
      start_frame: int, the first frame to export
      end_frame: int, the last frame to export

    Returns:
      tuple:
        - list of possible locations where the .vrscene may be found (Vray adds
          layer names automatically and is sometimes inconsistent)
        - dict of render job parameters, with any modifications to make the
          job run similarly with Vray standalone.
    """
    cmds.undoInfo(openChunk=True)

    _switch_to_renderlayer(layer)

    scene_path = cmds.file(q=True, loc=True)
    scene_head, extension = os.path.splitext(scene_path)
    scene_name = os.path.basename(scene_head)

    render_params = copy.deepcopy(params)

    render_params['project_dir'] = params['project']
    render_params['output_dir'] = params['out_path']
    render_params['use_nightly'] = params['vray_nightly']
    if ('extension' not in params['scene_info'] or
        params['scene_info']['extension'] == None or
        params['scene_info']['extension'].strip() == ''):
      render_params['scene_info']['extension'] = 'png'

    tail = cmds.getAttr(NamePrefixAttributes.vray)
    if not tail:
      tail = scene_name
      if len(params['layers'].split(',')) > 1:
        tail += '_{}'.format(layer)
    else:
      clean_camera = render_params['camera'].replace(':', '_')
      tail = replace_tokens_in_file_prefix(tail, scene_name, layer, clean_camera)
    if tail[-1] != '.':
      tail += '.'

    render_params['output_filename'] = '%s.%s' % (tail, render_params['scene_info']['extension'])
    render_params['output_filename'] = render_params['output_filename'].replace('\\', '/')

    # Set up render globals for vray export. These changes will
    # be reverted later when we run cmds.undo().
    #
    # Turn "Don't save image" OFF - this will ensure Vray knows to translate
    # all render output settings.
    cmds.setAttr('vraySettings.dontSaveImage', 0)
    # Turn rendering off.
    cmds.setAttr('vraySettings.vrscene_render_on', 0)
    # Turn Vrscene export on.
    cmds.setAttr('vraySettings.vrscene_on', 1)
    # Set the Vrscene export filename.
    cmds.setAttr('vraySettings.vrscene_filename', vrscene_path, type='string')
    # Ensure we export only a single file.
    cmds.setAttr('vraySettings.misc_separateFiles', 0)
    cmds.setAttr('vraySettings.misc_eachFrameInFile', 0)

    # Turn off Geom Cache. If you render a frame locally with this on, and then
    # immediately export to zync, the cached geometry is written to the file.
    # Any geo that has deformations are only rendered in the cached state and
    # not updated per frame. This is an issue with Vray and using 'vrend' instead
    # of BatchRender to export the vrscene.
    try:
      cmds.setAttr('vraySettings.globopt_cache_geom_plugins', 0)
      cmds.setAttr('vraySettings.globopt_cache_bitmaps', 0)
    # older versions of Vray do not have these settings. if they don't exist a
    # RuntimeError will be raised, which we can ignore.
    except RuntimeError:
      pass

    # Set compression options.
    cmds.setAttr('vraySettings.misc_meshAsHex', 1)
    cmds.setAttr('vraySettings.misc_transformAsHex', 1)
    cmds.setAttr('vraySettings.misc_compressedVrscene', 1)
    # Turn the VFB off, make sure the viewer is hidden.
    cmds.setAttr('vraySettings.vfbOn', 0)
    cmds.setAttr('vraySettings.hideRVOn', 1)
    # Ensure animation is fully enabled and configured with the correct
    # frame range. This is usually the case already, but some users will
    # have it disabled expecting their existing local farm to update
    # with the correct settings.
    cmds.setAttr('vraySettings.animBatchOnly', 0)
    cmds.setAttr('defaultRenderGlobals.animation', 1)
    cmds.setAttr('defaultRenderGlobals.startFrame', start_frame)
    cmds.setAttr('defaultRenderGlobals.endFrame', end_frame)
    # Set resolution of the scene to layer resolution to avoid problems with regions.
    cmds.setAttr('vraySettings.width', render_params['xres'])
    cmds.setAttr('vraySettings.height', render_params['yres'])

    # Run the export.
    maya.mel.eval('vrend -camera "%s" -layer "%s"' % (render_params['camera'], layer))

    queue_empty = cmds.undoInfo(query=True, undoQueueEmpty=True)
    cmds.undoInfo(closeChunk=True)
    if not queue_empty:
      cmds.undo()

    vrscene_base, ext = os.path.splitext(vrscene_path)
    if layer == 'defaultRenderLayer':
      possible_scene_names = [
        '%s_masterLayer%s' % (vrscene_base, ext),
        '%s%s' % (vrscene_base, ext),
        '%s_defaultRenderLayer%s' % (vrscene_base, ext)
      ]
    else:
      possible_scene_names = [
        '%s_%s%s' % (vrscene_base, layer, ext),
        vrscene_path,
      ]
      # In older Vray versions rs_ prefix is used for rendersetup layers, in
      # later versions it is ignored.
      if layer.startswith('rs_'):
        possible_scene_names.append('%s_%s%s' % (vrscene_base, layer[3:], ext))

    return possible_scene_names, render_params

  @staticmethod
  def export_ass(ass_path, layer, params, start_frame, end_frame):
    """Export .ass files of the current scene.

    Args:
      ass_path: str, path to which to export the .ass files
      layer: str, the name of the render layer to export
      params: dict, render job parameters
      start_frame: int, the first frame to export
      end_frame: int, the last frame to export

    Returns:
      tuple:
        - str path to the final export location. will contain a wildcard in
          place of frame number, to indicate the set of files produced.
        - dict of render job parameters, with any modifications to make the
          job run similarly with Arnold standalone.
    """
    cmds.undoInfo(openChunk=True)

    _switch_to_renderlayer(layer)

    scene_path = cmds.file(q=True, loc=True)
    scene_head, extension = os.path.splitext(scene_path)
    scene_name = os.path.basename(scene_head)

    render_params = copy.deepcopy(params)

    render_params['project_dir'] = params['project']
    render_params['output_dir'] = params['out_path']

    tail = cmds.getAttr(NamePrefixAttributes.arnold)
    if not tail:
      tail = scene_name
      if len(params['layers'].split(',')) > 1:
        tail += '_{}'.format(layer)
    else:
      clean_camera = params['camera'].replace(':', '_')
      tail = replace_tokens_in_file_prefix(tail, scene_name, layer, clean_camera)
      try:
        render_version = cmds.getAttr('defaultRenderGlobals.renderVersion')
        if render_version != None:
          tail = re.sub('%v|<version>',
            cmds.getAttr('defaultRenderGlobals.renderVersion'),
            tail, flags=re.IGNORECASE)
      except ValueError:
        pass
    if tail[-1] != '.':
      tail += '.'

    render_params['output_filename'] = '%s.%s' % (tail, params['scene_info']['extension'])
    render_params['output_filename'] = render_params['output_filename'].replace('\\', '/')

    ass_base, ext = os.path.splitext(ass_path)
    layer_mangled = base64.b64encode(layer)[-4:]
    layer_file = '%s_%s_%s%s' % (ass_base, layer, layer_mangled, ext)
    layer_file_wildcard = '%s_%s*%s' % (ass_base, layer, ext)

    # Override renderSetup options to keep exported *.ass files names consistent
    # with what backend expects (filename.framenumber.ext), see b/128825029
    maya.cmds.setAttr('defaultRenderGlobals.putFrameBeforeExt', 1)
    maya.cmds.setAttr('defaultRenderGlobals.periodInExt', 1)
    ass_cmd = ('arnoldExportAss -f "%s" -endFrame %s -mask 255 ' % (layer_file, end_frame) +
      '-lightLinks 1 -frameStep %d.0 -startFrame %s ' % (render_params['step'], start_frame) +
      '-shadowLinks 1 -cam %s' % (params['camera'],))
    maya.mel.eval(ass_cmd)

    queue_empty = cmds.undoInfo(query=True, undoQueueEmpty=True)
    cmds.undoInfo(closeChunk=True)
    if not queue_empty:
      cmds.undo()

    return layer_file_wildcard, render_params

  def get_standalone_scene_path(self, suffix, layer=None):
    """Get a file path for exporting a standalone scene, based on current scene
    and matching the Zync convention of where these files should be stored.

    This does NOT perform the actual export, only returns the path at which
    it should be stored.

    Args:
      suffix: str, the suffix of the filename e.g. "vrscene" or "ass"
      layer: str, the layer name to append to the file path

    Returns:
      str the standalone scene file path
    """
    scene_path = cmds.file(q=True, loc=True)
    scene_head, _ = os.path.splitext(scene_path)
    if layer is not None:
      scene_head += '_%s' % layer
    return self.zync_conn.generate_file_path(
        '%s.%s' % (scene_head, suffix)).replace('\\', '/')

  def verify_eula_acceptance(self, is_mayaio_job):
    """Verify EULA/ToS acceptance and if needed perform acceptance flow.

    Args:
      is_mayaio_job: bool, whether the intended job will make use of Maya I/O.

    Returns:
      bool, True if all required agreements are accepted, False if user declined
    """
    applicable_eula_types = ['zync', 'cloud', 'licensor']
    if is_mayaio_job:
      applicable_eula_types.append('mayaio')
    to_accept = [eula for eula in self.zync_conn.get_eulas()
                 if eula.get('eula_kind').lower() in applicable_eula_types]
    # Blank accepted_by field indicates agreement is not yet accepted.
    not_accepted = [eula for eula in to_accept if not eula.get('accepted_by')]
    if not_accepted:
      eula_url = '%s/account#legal' % self.zync_conn.url
      cmds.confirmDialog(
          title='Accept Agreement',
          message=(
              'Please read and accept the required EULA(s) and Terms of Service(s). '
              'A browser window will open where you can do this.\n\nURL: %s' % eula_url),
          button=['OK'],
          defaultButton='OK')
      webbrowser.open(eula_url)
      eula_response = cmds.confirmDialog(
          title='Accept Agreement',
          message='Have you accepted all agreements?',
          button=['Yes', 'No'],
          defaultButton='Yes',
          cancelButton='No',
          dismissString='No')

      if eula_response == 'No':
        return False

    return True


@show_exceptions
def submit_dialog():
  submit_window = SubmitWindow()
  submit_window.show()
  # show update notification last so it gets focus
  if not is_latest_version():
    show_update_notification()


def is_latest_version():
  global _VERSION_CHECK_RESULT
  if _VERSION_CHECK_RESULT is None:
    try:
      import_zync_python()
      _VERSION_CHECK_RESULT = zync.is_latest_version([('zync_maya', __version__)])
    # if there's an exception during version check, print the exception but
    # assume user is up to date. we don't want to block them launching jobs.
    except:
      print 'Exception checking version number'
      print traceback.format_exc()
      return True
  return _VERSION_CHECK_RESULT


def replace_tokens_in_file_prefix(file_prefix, scene_name, layer, camera):
  """
  Replace various tokens in the file output prefix with values from the scene.

  Args:
    file_prefix: str, string containing tokens to be replaced.
    scene_name: str, name of scene file to replace _SUBSTITUTE_SCENE_TOKEN_RE.
    layer: str, name of layer to replace _SUBSTITUTE_LAYER_TOKEN_RE.
    camera: str, name of camera to replace _SUBSTITUTE_CAMERA_TOKEN_RE.

  Returns:
      str, token replaced file prefix.
  """
  mappings = (
    (maya_common._SUBSTITUTE_SCENE_TOKEN_RE, scene_name),
    (maya_common._SUBSTITUTE_LAYER_TOKEN_RE, layer),
    (maya_common._SUBSTITUTE_CAMERA_TOKEN_RE, camera),
  )
  for regex, value in mappings:
    file_prefix = re.sub(regex, value, file_prefix)
  return file_prefix


def output_has_layer_problems(renderer, layer_list):
  """
  Submission check to ensure a layer token (%l, <layer>, or <renderlayer>) exists in render file name output attribute
  and that there are multiple render layer in the layer_list. If the prefix is empty, return False since we'll take
  care of setting the output path.

  Args:
      renderer: str, name of renderer.
      layer_list: [str], list of string names of layers to be checked.

  Returns:
      bool, True if outputs are problematic False if outputs are safe
  """
  try:
    output_prefix = cmds.getAttr(NamePrefixAttributes.get_prefix(renderer))
  except AttributeError:
    raise maya_common.MayaZyncException('Renderer %s unsupported for rendering.' % renderer)
  if output_prefix is None:
    return False
  return len(layer_list) > 1 and not re.match(maya_common._HAS_LAYER_TOKEN_RE, output_prefix)


def show_update_notification():
  def _link(url, text):
    return ('<a style="color:#ff8a00;" href="%s">%s</a>') % (url, text)

  window_name = cmds.window(title='Zync Update Available', width=400, height=165)

  cmds.columnLayout('l', rowSpacing=8, columnAttach=('both', 100))
  cmds.text(label='<br>An update to the Zync plugin has<br>been released.',
            align="center", width=200)
  cmds.text(label=_link('https://download.zyncrender.com', 'Download the Update'),
            hyperlink=True, align="center", width=200)
  cmds.text(label=_link('https://docs.zyncrender.com/update-plugins',
            'Plugin Update HOWTO'), hyperlink=True, align="center", width=200)
  cmds.text(label='  Once the update is installed, please<br>restart Maya to complete the process.',
            align="center", width=200)
  cmds.button(label='Close', width=200, align='center',
              command='cmds.deleteUI("%s", window=True)' % window_name)

  cmds.showWindow()
