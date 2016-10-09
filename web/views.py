import re
import sys
import json
from datetime import datetime
from web.common import *
import multiprocessing
from common import Config, checksum_md5
try:
    from Registry import Registry
except ImportError:
    pass
config = Config()

logger = logging.getLogger(__name__)

try:
    from bson.objectid import ObjectId
except ImportError:
    logger.error('Unable to import pymongo')

from django.shortcuts import render, redirect
from django.http import HttpResponse, JsonResponse, HttpResponseServerError, StreamingHttpResponse
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.views.decorators.csrf import csrf_exempt

try:
    import virus_total_apis
    from virus_total_apis import PublicApi
    VT_LIB = True
    # Version check needs to be higher than 1.0.9
    vt_ver = virus_total_apis.__version__.split('.')
    if int(vt_ver[1]) < 1:
        logger.warning("virustotal-api version is too low. 'sudo pip install --upgrade virustotal-api'")
        VT_LIB = False
except ImportError:
    VT_LIB = False
    logger.warning("Unable to import VirusTotal API Library")

try:
    import yara
    YARA = True
except ImportError:
    YARA = False
    logger.warning("Unable to import Yara")

##
# Import The volatility Interface and DB Class
##
import vol_interface
from vol_interface import RunVol

try:
    from web.database import Database
    db = Database()
except Exception as e:
    logger.error("Unable to access mongo database: {0}".format(e))

##
# Registry Stuffs
##


def reg_sub_keys(key):
    sub_keys = []

    for subkey in key.subkeys():
        sub_keys.append(subkey)

    return sub_keys


def reg_key_values(key):
    key_values = []
    for value in [v for v in key.values()
                  if v.value_type() == Registry.RegSZ or v.value_type() == Registry.RegExpandSZ]:
        key_values.append([value.name(), value.value()])
    return key_values


def session_creation(request, mem_image, session_id):
    # Get some vars

    new_session = db.get_session(session_id)

    file_hash = False
    if 'description' in request.POST:
        new_session['session_description'] = request.POST['description']
    if 'plugin_path' in request.POST:
        new_session['plugin_path'] = request.POST['plugin_path']
    if 'file_hash' in request.POST:
        file_hash = True

    # Check for mem file
    if not os.path.exists(mem_image):
        logger.error('Unable to find an image file at {0}'.format(mem_image))
        return main_page(request, error_line='Unable to find an image file at {0}'.format(request.POST['sess_path']))

    new_session['session_path'] = mem_image

    # Generate FileHash (MD5 for now)
    if file_hash:
        logger.debug('Generating MD5 for Image')
        # Update the status
        new_session['status'] = 'Calculating MD5'
        db.update_session(session_id, new_session)

        md5_hash = checksum_md5(new_session['session_path'])
        new_session['file_hash'] = md5_hash

    # Get a list of plugins we can use. and prepopulate the list.

    if 'profile' in request.POST:
        if request.POST['profile'] != 'AutoDetect':
            profile = request.POST['profile']
            new_session['session_profile'] = profile
        else:
            profile = None
    else:
        profile = None

    vol_int = RunVol(profile, new_session['session_path'])

    image_info = {}

    if not profile:
        logger.debug('AutoDetecting Profile')
        # kdbg scan to get a profile suggestion
        # Update the status
        new_session['status'] = 'Detecting Profile'
        db.update_session(session_id, new_session)
        # Doesnt support json at the moment
        kdbg_results = vol_int.run_plugin('kdbgscan', output_style='text')

        lines = kdbg_results['rows'][0][0]

        profiles = []

        for line in lines.split('\n'):
            if 'Profile suggestion' in line:
                profiles.append(line.split(':')[1].strip())

        if len(profiles) == 0:
            logger.error('Unable to find a valid profile with kdbg scan')
            return main_page(request, error_line='Unable to find a valid profile with kdbg scan')

        profile = profiles[0]

        # Re initialize with correct profile
        vol_int = RunVol(profile, new_session['session_path'])

    # Get compatible plugins

    plugin_list = vol_int.list_plugins()

    new_session['session_profile'] = profile

    new_session['image_info'] = image_info

    # Plugin Options
    plugin_filters = vol_interface.plugin_filters

    # Update Session
    new_session['status'] = 'Complete'
    db.update_session(session_id, new_session)

    # Autorun list from config
    if config.autorun == 'True':
        auto_list = config.plugins.split(',')
    else:
        auto_list = False

    # Merge Autorun from manual post with config
    if 'auto_run' in request.POST:
        run_list = request.POST['auto_run'].split(',')
        if not auto_list:
            auto_list = run_list
        else:
            for run in run_list:
                if run not in auto_list:
                    auto_list.append(run)

    # For each plugin create the entry
    for plugin in plugin_list:
        plugin_name = plugin[0]
        db_results = {'session_id': session_id, 'plugin_name': plugin_name}

        # Ignore plugins we cant handle
        if plugin_name in plugin_filters['drop']:
            continue

        db_results['help_string'] = plugin[1]
        db_results['created'] = None
        db_results['plugin_output'] = None
        db_results['status'] = None
        # Write to DB
        plugin_id = db.create_plugin(db_results)

        if auto_list:
            if plugin_name in auto_list:
                multiprocessing.Process(target=run_plugin, args=(session_id, plugin_id)).start()


##
# Page Views
##

def main_page(request, error_line=None):
    """
    Returns the main vol page
    :param request:
    :param error_line:
    :return:
    """

    # Check Vol Version

    try:
        vol_ver = vol_interface.vol_version.split('.')
        if int(vol_ver[1]) < 5:
            error_line = 'UNSUPPORTED VOLATILITY VERSION. REQUIRES 2.5 FOUND {0}'.format(vol_interface.vol_version)
    except Exception as error:
        error_line = 'Unable to find a volatility version'
        logger.error(error_line)

    # Set Pagination
    page = request.GET.get('page')
    if not page:
        page = 1
    page_count = request.GET.get('count')
    if not page_count:
        page_count = 30

    # Get All Sessions
    session_list = db.get_allsessions()

    # Paginate
    session_count = len(session_list)
    first_session = int(page) * int(page_count) - int(page_count) + 1
    last_session = int(page) * int(page_count)

    paginator = Paginator(session_list, page_count)

    try:
        sessions = paginator.page(page)
    except PageNotAnInteger:
        sessions = paginator.page(1)
    except EmptyPage:
        sessions = paginator.page(paginator.num_pages)

    # Show any extra loaded plugins
    plugin_dirs = []
    if os.path.exists(volrc_file):
        vol_conf = open(volrc_file, 'r').readlines()
        for line in vol_conf:
            if line.startswith('PLUGINS'):
                plugin_dirs = line.split(' = ')[-1]

    # Profile_list for add session
    RunVol('', '')
    profile_list = vol_interface.profile_list()

    return render(request, 'index.html', {'session_list': sessions,
                                          'session_counts': [session_count, first_session, last_session],
                                          'profile_list': profile_list,
                                          'plugin_dirs': plugin_dirs,
                                          'error_line': error_line
                                          })


def session_page(request, sess_id):
    """
    returns the session page thats used to run plugins
    :param request:
    :param sess_id:
    :return:
    """
    error_line = False

    # Check Vol Version
    if float(vol_interface.vol_version) < 2.5:
        error_line = 'UNSUPPORTED VOLATILITY VERSION. REQUIRES 2.5 FOUND {0}'.format(vol_interface.vol_version)

    # Get the session
    session_id = ObjectId(sess_id)
    session_details = db.get_session(session_id)
    comments = db.get_commentbysession(session_id)
    extra_search = db.search_files({'file_meta': 'ExtraFile', 'sess_id': session_id})
    extra_files = []
    for upload in extra_search:
        extra_files.append({'filename': upload.filename, 'file_id': upload._id})

    plugin_list = []
    yara_list = os.listdir('yararules')
    plugin_text = db.get_pluginbysession(ObjectId(sess_id))
    version_info = {'python': str(sys.version).split()[0],
                    'volatility': vol_interface.vol_version,
                    'volutility': volutility_version}

    # Check if file still exists

    if not os.path.exists(session_details['session_path']):
        error_line = 'Memory Image can not be found at {0}'.format(session_details['session_path'])

    return render(request, 'session.html', {'session_details': session_details,
                                            'plugin_list': plugin_list,
                                            'plugin_output': plugin_text,
                                            'comments': comments,
                                            'error_line': error_line,
                                            'version_info': version_info,
                                            'yara_list': yara_list,
                                            'extra_files': extra_files})


def create_session(request):
    """
    post handler to create a new session
    :param request:
    :return:
    """

    if 'process_dir' in request.POST:
        recursive_dir = True
    else:
        recursive_dir = False

    dir_listing = []

    if not 'sess_path' in request.POST:
        logger.error('No path or file selected')
        return main_page(request, error_line='No path or file selected')


    if recursive_dir:
        for root, subdir, filename in os.walk(request.POST['sess_path']):
            for name in filename:
                # ToDo: Add extension check
                extensions = ['bin', 'mem', 'img', '001', 'raw', 'dmp', 'vmem']
                for ext in extensions:
                    if name.lower().endswith(ext):
                        dir_listing.append(os.path.join(root, name))

    else:
        dir_listing.append(request.POST['sess_path'])

    for mem_image in dir_listing:

        # Create session in DB and set to pending
        new_session = {'created': datetime.now(),
                       'modified': datetime.now(),
                       'file_hash': 'Not Selected',
                       'status': 'Processing',
                       'session_profile': request.POST['profile']
                       }

        if 'sess_name' in request.POST:
            new_session['session_name'] = '{0} ({1})'.format(request.POST['sess_name'], mem_image.split('/')[-1])
        else:
            new_session['session_name'] = mem_image.split('/')[-1]

        # Store it
        session_id = db.create_session(new_session)

        # Run the multiprocessing
        multiprocessing.Process(target=session_creation, args=(request, mem_image, session_id)).start()

        # Add search all on main page filter sessions that match.

    return redirect('/')


def run_plugin(session_id, plugin_id, pid=None, plugin_options=None):
    """
    return the results json from a plugin
    :param session_id:
    :param plugin_id:
    :param pid:
    :param plugin_options:
    :return:
    """
    dump_dir = None
    error = None
    plugin_id = ObjectId(plugin_id)
    sess_id = ObjectId(session_id)
    if pid:
        pid = str(pid)

    if sess_id and plugin_id:
        # Get details from the session
        session = db.get_session(sess_id)
        # Get details from the plugin
        plugin_row = db.get_pluginbyid(ObjectId(plugin_id))

        plugin_name = plugin_row['plugin_name'].lower()

        logger.debug('Running Plugin: {0}'.format(plugin_name))

        # Set plugin status
        new_values = {'status': 'processing'}
        db.update_plugin(ObjectId(plugin_id), new_values)

        # set vol interface
        vol_int = RunVol(session['session_profile'], session['session_path'])

        # Run the plugin with json as normal
        output_style = 'json'
        try:
            results = vol_int.run_plugin(plugin_name,
                                         output_style=output_style,
                                         pid=pid,
                                         plugin_options=plugin_options
                                         )
        except Exception as error:
            results = False
            logger.error('Json Output error in {0} - {1}'.format(plugin_name, error))

        if 'unified output format has not been implemented' in str(error) or 'JSON output for trees' in str(error):
            output_style = 'text'
            try:
                results = vol_int.run_plugin(plugin_name,
                                             output_style=output_style,
                                             pid=pid,
                                             plugin_options=plugin_options
                                             )
                error = None
            except Exception as error:
                logger.error('Json Output error in {0}, {1}'.format(plugin_name, error))
                results = False

        # If we need a DumpDir
        if '--dump-dir' in str(error) or 'specify a dump directory' in str(error):
            # Create Temp Dir
            logger.debug('{0} - Creating Temp Directory'.format(plugin_name))
            temp_dir = tempfile.mkdtemp()
            dump_dir = temp_dir
            try:
                results = vol_int.run_plugin(plugin_name,
                                             dump_dir=dump_dir,
                                             output_style=output_style,
                                             pid=pid,
                                             plugin_options=plugin_options
                                             )
            except Exception as error:
                results = False
                # Set plugin status
                new_values = {'status': 'error'}
                db.update_plugin(ObjectId(plugin_id), new_values)
                logger.error('Error: Unable to run plugin {0} - {1}'.format(plugin_name, error))

        # Check for result set
        if not results:
            # Set plugin status
            new_values = {'status': 'completed'}
            db.update_plugin(ObjectId(plugin_id), new_values)
            return 'Warning: No output from Plugin {0}'.format(plugin_name)

        ##
        # Files that dump output to disk
        ##

        if dump_dir:
            file_list = os.listdir(dump_dir)
            '''
            I need to process the results and the items in the dump dir.

            Add Column for ObjectId

            Store the file in the GridFS get an ObjectId
            add the ObjectId to the rows, each has a differnet column format so this could be a pain.

            '''

            # Add Rows

            if plugin_row['plugin_name'] == 'dumpfiles':
                if not plugin_row['plugin_output']:
                    results = {'columns': ['Offset', 'File Name', 'Image Type', 'StoredFile'], 'rows': []}
                else:
                    results = plugin_row['plugin_output']

                for filename in file_list:
                    if filename.endswith('img'):
                        img_type = 'ImageSectionObject'
                    elif filename.endswith('dat'):
                        img_type = 'DataSectionObject'
                    elif filename.endswith('vacb'):
                        img_type = 'SharedCacheMap'
                    else:
                        img_type = 'N/A'
                    file_data = open(os.path.join(dump_dir, filename), 'rb').read()
                    sha256 = hashlib.sha256(file_data).hexdigest()
                    file_id = db.create_file(file_data, sess_id, sha256, filename)
                    results['rows'].append([plugin_options['PHYSOFFSET'],
                                            filename,
                                            img_type,
                                            '<a class="text-success" href="#" '
                                            'onclick="ajaxHandler(\'filedetails\', {\'file_id\':\'' +
                                            str(file_id) + '\'}, false ); return false">'
                                            'File Details</a>'])

            if plugin_row['plugin_name'] in ['procdump', 'dlldump']:
                # Add new column
                results['columns'].append('StoredFile')
                for row in results['rows']:
                    if row[-1].startswith("OK"):
                        filename = row[-1].split("OK: ")[-1]
                        if filename in file_list:
                            file_data = open(os.path.join(dump_dir, filename), 'rb').read()
                            sha256 = hashlib.sha256(file_data).hexdigest()
                            file_id = db.create_file(file_data, sess_id, sha256, filename)
                            row.append('<a class="text-success" href="#" '
                                       'onclick="ajaxHandler(\'filedetails\', {\'file_id\':\'' + str(file_id) +
                                       '\'}, false ); return false">'
                                       'File Details</a>')
                    else:
                        row.append('Not Stored')

            if plugin_row['plugin_name'] == 'dumpregistry':
                results = {'columns': ['Hive Name', 'StoredFile'], 'rows': []}
                for filename in file_list:
                    file_data = open(os.path.join(dump_dir, filename), 'rb').read()
                    sha256 = hashlib.sha256(file_data).hexdigest()
                    file_id = db.create_file(file_data, sess_id, sha256, filename)
                    results['rows'].append([filename,
                                            '<a class="text-success" href="#" '
                                            'onclick="ajaxHandler(\'filedetails\', {\'file_id\':\'' + str(file_id) +
                                            '\'}, false ); return false">'
                                            'File Details</a>'])

            if plugin_row['plugin_name'] in ['dumpcerts']:
                # Add new column
                for row in results['rows']:
                    filename = row[5]
                    if filename in file_list:
                        file_data = open(os.path.join(dump_dir, filename), 'rb').read()
                        sha256 = hashlib.sha256(file_data).hexdigest()
                        file_id = db.create_file(file_data, sess_id, sha256, filename)
                        row[-1] = '<a class="text-success" href="#" ' \
                                  'onclick="ajaxHandler(\'filedetails\', {\'file_id\':\'' + \
                                  str(file_id) + '\'}, false ); return false">' \
                                  'File Details</a>'
                    else:
                        row.append('Not Stored')

            if plugin_row['plugin_name'] in ['memdump']:
                logger.debug('Processing Rows')
                # Convert text to rows
                if not plugin_row['plugin_output']:
                    new_results = {'rows': [], 'columns': ['Process', 'PID', 'StoredFile']}
                else:
                    new_results = plugin_row['plugin_output']
                base_output = results['rows'][0][0]
                base_output = base_output.lstrip('<pre>').rstrip('</pre>')
                for line in base_output.split('*'*72):
                    if '.dmp' not in line:
                        continue
                    row = line.split()
                    process = row[1]
                    dump_file = row[-1]
                    pid = dump_file.split('.')[0]

                    if dump_file not in file_list:
                        new_results['rows'].append([process, pid, 'Not Stored'])
                    else:
                        logger.debug('Store memdump file')
                        file_data = open(os.path.join(dump_dir, dump_file), 'rb').read()
                        sha256 = hashlib.sha256(file_data).hexdigest()
                        file_id = db.create_file(file_data, sess_id, sha256, dump_file)
                        row_file = '<a class="text-success" href="#" ' \
                                   'onclick="ajaxHandler(\'filedetails\', {\'file_id\':\'' + str(file_id) + \
                                   '\'}, false ); return false">' \
                                   'File Details</a>'
                        new_results['rows'].append([process, pid, row_file])

                results = new_results

            # ToDo
            '''
            if plugin_row['plugin_name'] in ['malfind']:
                logger.debug('Processing Rows')
                # Convert text to rows
                new_results = plugin_row['plugin_output']

                if len(file_list) == 0:
                    new_results['rows'].append([process, pid, 'Not Stored'])
                else:
                    for dump_file in file_list:
                        logger.debug('Store memdump file')
                        file_data = open(os.path.join(temp_dir, dump_file), 'rb').read()
                        sha256 = hashlib.sha256(file_data).hexdigest()
                        file_id = db.create_file(file_data, sess_id, sha256, dump_file)
                        row_file = '<a class="text-success" href="#" ' \
                              'onclick="ajaxHandler(\'filedetails\', {\'file_id\':\'' + str(file_id) + '\'}, false ); return false">' \
                              'File Details</a>'
                        new_results['rows'].append([process, pid, row_file])

                results = new_results
            '''

            # Remove the dumpdir
            shutil.rmtree(dump_dir)

        ##
        # Extra processing output
        # Do everything in one loop to save time
        ##

        if results:
            # Start Counting
            counter = 1

            # Columns

            # Add Row ID Column
            if results['columns'][0] != '#':
                results['columns'].insert(0, '#')

            # Add option to process hive keys
            if plugin_row['plugin_name'] in ['hivelist', 'hivescan']:
                results['columns'].append('Extract Keys')

            # Add option to process malfind
            if plugin_row['plugin_name'] in ['malfind']:
                results['columns'].append('Extract Injected Code')

            # Now Rows
            for row in results['rows']:
                # Add Row ID
                if plugin_name == 'memdump':
                    if len(row) == 3:
                        row.insert(0, counter)
                elif plugin_name == 'dumpfiles':
                    if len(row) == 4:
                        row.insert(0, counter)
                else:
                    row.insert(0, counter)

                if plugin_row['plugin_name'] in ['hivelist', 'hivescan']:
                    ajax_string = "onclick=\"ajaxHandler('hivedetails', {'plugin_id':'" + str(plugin_id) + \
                                  "', 'rowid':'" + str(counter) + "'}, true )\"; return false"
                    row.append('<a class="text-success" href="#" ' + ajax_string + '>View Hive Keys</a>')

                # Add option to process malfind
                if plugin_row['plugin_name'] in ['malfind']:
                    ajax_string = "onclick=\"ajaxHandler('malfind_export', {'plugin_id':'" + str(plugin_id) + \
                                  "', 'rowid':'" + str(counter) + "'}, true )\"; return false"
                    row.append('<a class="text-success" href="#" ' + ajax_string + '>Extract Injected</a>')

                counter += 1

        # Image Info

        image_info = False
        if plugin_name == 'imageinfo':
            imageinfo_text = results['rows'][0][1]
            image_info = {}
            for line in imageinfo_text.split('\n'):
                try:
                    key, value = line.split(' : ')
                    image_info[key.strip()] = value.strip()
                except Exception as error:
                    print 'Error Getting imageinfo: {0}'.format(error)

        # update the plugin
        new_values = {'created': datetime.now(), 'plugin_output': results, 'status': 'completed'}

        try:
            db.update_plugin(ObjectId(plugin_id), new_values)
            # Update the session
            new_sess = {'modifed': datetime.now()}
            if image_info:
                new_sess['image_info'] = image_info
            db.update_session(sess_id, new_sess)

            return plugin_row['plugin_name']

        except Exception as error:
            # Set plugin status
            new_values = {'status': 'error'}
            db.update_plugin(ObjectId(plugin_id), new_values)
            logger.error('Error: Unable to Store Output for {0} - {1}'.format(plugin_name, error))
            return 'Error: Unable to Store Output for {0} - {1}'.format(plugin_name, error)


def file_download(request, query_type, object_id):
    """
    return a file from the gridfs by id
    :param request:
    :param query_type:
    :param object_id:
    :return:
    """

    if query_type == 'file':
        file_object = db.get_filebyid(ObjectId(object_id))
        file_name = '{0}.bin'.format(file_object.filename)
        response = StreamingHttpResponse((chunk for chunk in file_object), content_type='application/octet-stream')
        response['Content-Disposition'] = 'attachment; filename="{0}"'.format(file_name)
        return response

    if query_type == 'plugin':
        plugin_object = db.get_pluginbyid(ObjectId(object_id))

        file_name = '{0}.csv'.format(plugin_object['plugin_name'])
        plugin_data = plugin_object['plugin_output']

        file_data = ""
        file_data += ",".join(plugin_data['columns'])
        file_data += "\n"
        for row in plugin_data['rows']:
            for item in row:
                file_data += "{0},".format(item)
            file_data.rstrip(',')
            file_data += "\n"

        response = HttpResponse(file_data, content_type='application/octet-stream')
        response['Content-Disposition'] = 'attachment; filename="{0}"'.format(file_name)
        return response


@csrf_exempt
def addfiles(request):
    for k, v in request.POST.iteritems():
        print k, v

    if 'session_id' not in request.POST:
        logger.warning('No Session ID in POST')
        return HttpResponseServerError

    session_id = ObjectId(request.POST['session_id'])

    for upload in request.FILES.getlist('files[]'):
        logger.debug('Storing File: {0}'.format(upload.name))
        file_data = upload.read()
        sha256 = hashlib.sha256(file_data).hexdigest()

        # Store file in GridFS
        db.create_file(file_data, session_id, sha256, upload.name, pid=None, file_meta='ExtraFile')

    # Return the new list
    extra_search = db.search_files({'file_meta': 'ExtraFile', 'sess_id': session_id})
    extra_files = []
    for upload in extra_search:
        extra_files.append({'filename': upload.filename, 'file_id': upload._id})

    return render(request, 'file_upload_table.html', {'extra_files': extra_files})


@csrf_exempt
def ajax_handler(request, command):
    """
    return data requested by the ajax handler in volutility.js
    :param request:
    :param command:
    :return:
    """

    if command == 'pollplugins':
        if 'session_id' in request.POST:
            # Get Current Session
            session_id = request.POST['session_id']
            session = db.get_session(ObjectId(session_id))
            plugin_rows = db.get_pluginbysession(ObjectId(session_id))

            # Check for new registered plugins
            # Get compatible plugins

            profile = session['session_profile']
            session_path = session['session_path']
            vol_int = RunVol(profile, session_path)
            plugin_list = vol_int.list_plugins()

            # Plugin Options
            plugin_filters = vol_interface.plugin_filters
            refresh_rows = False
            existing_plugins = []
            for row in plugin_rows:
                existing_plugins.append(row['plugin_name'])

            # For each plugin create the entry
            for plugin in plugin_list:

                # Ignore plugins we cant handle
                if plugin[0] in plugin_filters['drop']:
                    continue

                if plugin[0] in existing_plugins:
                    continue

                else:
                    db_results = {'session_id': ObjectId(session_id),
                                  'plugin_name': plugin[0],
                                  'help_string': plugin[1],
                                  'created': None,
                                  'plugin_output': None,
                                  'status': None}
                    # Write to DB
                    db.create_plugin(db_results)
                    refresh_rows = True

            if refresh_rows:
                plugin_rows = db.get_pluginbysession(ObjectId(session_id))

            return render(request, 'plugin_poll.html', {'plugin_output': plugin_rows})
        else:
            return HttpResponseServerError

    if command == 'filtersessions':
        matching_sessions = []
        if ('pluginname' and 'searchterm') in request.POST:
            pluginname = request.POST['pluginname']
            searchterm = request.POST['searchterm']
            results = db.search_plugins(searchterm, plugin_name=pluginname)
            for row in results:
                matching_sessions.append(str(row))
        json_response = json.dumps(matching_sessions)

        return JsonResponse(matching_sessions, safe=False)


    if command == 'dropplugin':
        if 'plugin_id' in request.POST:
            plugin_id = request.POST['plugin_id']
            # update the plugin
            new_values = {'created': None, 'plugin_output': None, 'status': None}
            db.update_plugin(ObjectId(plugin_id), new_values)
            return HttpResponse('OK')

    if command == 'runplugin':
        print 1
        if 'plugin_id' in request.POST and 'session_id' in request.POST:
            plugin_name = run_plugin(request.POST['session_id'], request.POST['plugin_id'])
            return HttpResponse(plugin_name)

    if command == 'plugin_dir':

        # Platform PATH seperator
        seperator = ':'
        if sys.platform.startswith('win'):
            seperator = ';'

        # Set Plugins
        if 'plugin_dir' in request.POST:
            plugin_dir = request.POST['plugin_dir']

            if os.path.exists(volrc_file):
                with open(volrc_file, 'a') as out:
                    output = '{0}{1}'.format(seperator, plugin_dir)
                    out.write(output)
                return HttpResponse(' No Plugin Path Provided')
            else:
                # Create new file.
                with open(volrc_file, 'w') as out:
                    output = '[DEFAULT]\nPLUGINS = {0}'.format(plugin_dir)
                    out.write(output)
                return HttpResponse(' No Plugin Path Provided')
        else:
            return HttpResponse(' No Plugin Path Provided')

    if command == 'filedetails':
        if 'session_id' in request.POST:
            session_id = request.POST['session_id']
            session_details = db.get_session(ObjectId(session_id))

        if 'file_id' in request.POST:
            file_id = request.POST['file_id']
            file_object = db.get_filebyid(ObjectId(file_id))
            file_datastore = db.search_datastore({'file_id': ObjectId(file_id)})

            vt_results = None
            yara_match = None
            string_list = None
            state = 'notchecked'

            for row in file_datastore:

                if 'vt' in row:
                    vt_results = row['vt']
                    state = 'complete'
                if 'yara' in row:
                    yara_match = row['yara']

            # New String Store
            new_strings = db.get_strings(file_id)
            if new_strings:
                string_list = new_strings._id

            yara_list = sorted(os.listdir('yararules'))
            return render(request, 'file_details.html', {'file_details': file_object,
                                                         'file_id': file_id,
                                                         'yara_list': yara_list,
                                                         'yara': yara_match,
                                                         'vt_results': vt_results,
                                                         'string_list': string_list,
                                                         'state': state,
                                                         'error': None,
                                                         'session_details': session_details
                                                         })

    if command == 'hivedetails':
        if 'plugin_id' and 'rowid' in request.POST:
            pluginid = request.POST['plugin_id']
            rowid = request.POST['rowid']
            plugin_details = db.get_pluginbyid(ObjectId(pluginid))
            key_name = 'hive_keys_{0}'.format(rowid)

            if key_name in plugin_details:
                hive_details = plugin_details[key_name]
            else:
                session_id = plugin_details['session_id']

                session = db.get_session(session_id)

                plugin_data = plugin_details['plugin_output']

                hive_offset = None
                for row in plugin_data['rows']:
                    if str(row[0]) == rowid:
                        hive_offset = str(row[1])

                # Run the plugin
                vol_int = RunVol(session['session_profile'], session['session_path'])
                hive_details = vol_int.run_plugin('hivedump', hive_offset=hive_offset)

                # update the plugin / session
                new_values = {key_name: hive_details}
                db.update_plugin(ObjectId(ObjectId(pluginid)), new_values)
                # Update the session
                new_sess = {'modified': datetime.now()}
                db.update_session(session_id, new_sess)

            return render(request, 'hive_details.html', {'hive_details': hive_details})

    if command == 'hiveviewer':

        import urllib
        # https://github.com/williballenthin/python-registry
        file_id = request.POST['file_id']

        key_request = urllib.unquote(request.POST['key'])

        reg_data = db.get_filebyid(ObjectId(file_id))

        reg = Registry.Registry(reg_data)

        print key_request

        if key_request == 'root':
            key = reg.root()

        else:
            try:
                key = reg.open(key_request)
            except Registry.RegistryKeyNotFoundException:
                # Check for values
                key = False

        if key:
            # Get the Parent
            try:
                parent_path = "\\".join(key.parent().path().strip("\\").split('\\')[1:])
                print key.parent().path()
            except Registry.RegistryKeyHasNoParentException:
                parent_path = None


            json_response = {'parent_key': parent_path}

            # Get Sub Keys
            child_keys = []
            for sub in reg_sub_keys(key):
                sub_path = "\\".join(sub.path().strip("\\").split('\\')[1:])
                child_keys.append(sub_path)

            # Get Values
            key_values = []
            for value in key.values():

                val_name = value.name()
                val_type = value.value_type_str()
                val_value = value.value()

                # Replace Unicode Chars
                try:
                    val_value = val_value.replace('\x00', ' ')
                except AttributeError:
                    pass

                # Convert Bin to Hex chars

                if val_type == 'RegBin' and all(c in string.printable for c in val_value) == False:
                    val_value = val_value.encode('hex')

                if val_type == 'RegNone' and all(c in string.printable for c in val_value) == False:
                    val_value = val_value.encode('hex')

                # Assemble and send
                key_values.append([val_name, val_type, val_value])

                #print val_type, val_value

            json_response['child_keys'] = child_keys
            json_response['key_values'] = key_values

            json_response = json.dumps(json_response)

            return JsonResponse(json_response, safe=False)

        else:
            json_response = {}
            json_response['child_keys'] = []
            json_response['key_values'] = []
            return JsonResponse(json_response)


    if command == 'dottree':
        session_id = request.POST['session_id']
        # Check for existing Map
        dottree = db.search_datastore({'session_id': ObjectId(session_id)})
        if len(dottree) > 0:
            if 'dottree' in dottree[0]:
                print 'return Existing'
                return HttpResponse(dottree[0]['dottree'])

        # Else Generate and store
        session = db.get_session(ObjectId(session_id))
        vol_int = RunVol(session['session_profile'], session['session_path'])
        results = vol_int.run_plugin('pstree', output_style='dot')

        # Configure the output for svg with D3 and digraph-d3

        digraph = ''
        for line in results.split('\n'):
            if line.startswith('  #'):
                pass
            elif line.startswith('  node[shape'):
                digraph += '{0}\n'.format('  node [labelStyle="font: 300 20px \'Helvetica Neue\', Helvetica"]')
            elif 'label="{' in line:
                # Format each node
                node_block = re.search('\[label="{(.*)}"\]', line)
                node_text = node_block.group(1)
                elements = node_text.split('|')
                label_style = '<table> \
                                <tbody> \
                                <tr><td>Name</td><td>|Name|</td></tr> \
                                <tr><td>PID</td><td>|Pid|</td></tr> \
                                <tr><td>PPID</td><td>|PPid|</td></tr> \
                                <tr><td>Offfset</td><td>|Offset|</td></tr> \
                                <tr><td>Threads</td><td>|Thds|</td></tr> \
                                <tr><td>Handles</td><td>|Hnds|</td></tr> \
                                <tr><td>Time</td><td>|Time|</td></tr> \
                                </tbody> \
                                </table>'

                for elem in elements:
                    key, value = elem.split(':', 1)
                    label_style = label_style.replace('|{0}|'.format(key), value)

                line = line.replace('label="', 'labelType="html" label="')
                line = line.replace('{'+node_text+'}', label_style)
                digraph += '{0}\n'.format(line)

            else:
                digraph += '{0}\n'.format(line)

        # Store the results in datastore
        store_data = {'session_id': ObjectId(session_id),
                      'dottree': digraph}
        db.create_datastore(store_data)

        return HttpResponse(digraph)

    if command == 'timeline':
        logger.debug('Running Timeline')
        session_id = request.POST['session_id']
        session = db.get_session(ObjectId(session_id))
        vol_int = RunVol(session['session_profile'], session['session_path'])
        results = vol_int.run_plugin('timeliner', output_style='dot')

        # Configure the output for svg with D3 and digraph-d3

        digraph = ''
        for line in results.split('\n'):
            if line.startswith('  #'):
                pass
            elif line.startswith('  node[shape'):
                digraph += '{0}\n'.format('  node [labelStyle="font: 300 20px \'Helvetica Neue\', Helvetica"]')
            elif 'label="{' in line:
                # Format each node
                node_block = re.search('\[label="{(.*)}"\]', line)
                node_text = node_block.group(1)
                elements = node_text.split('|')

                label_style = '<table> \
                                <tbody> \
                                <tr><td>Start</td><td>|Start|</td></tr> \
                                <tr><td>Header</td><td>|Header|</td></tr> \
                                <tr><td>Item</td><td>|Item|</td></tr> \
                                <tr><td>Details</td><td>|Details|</td></tr> \
                                <tr><td>End</td><td>|End|</td></tr> \
                                </tbody> \
                                </table>'

                for elem in elements:
                    key, value = elem.split(':', 1)
                    label_style = label_style.replace('|{0}|'.format(key), value)

                line = line.replace('label="', 'labelType="html" label="')
                line = line.replace('{'+node_text+'}', label_style)
                digraph += '{0}\n'.format(line)

            else:
                digraph += '{0}\n'.format(line)

        return HttpResponse(results)

    if command == 'virustotal':
        if not config.api_key or not VT_LIB:
            logger.error('No Virustotal key provided in volutitliy.conf')
            return HttpResponse("Unable to use Virus Total. No Key or Library Missing. Check the Console for details")

        if 'file_id' in request.POST:
            file_id = request.POST['file_id']

            file_object = db.get_filebyid(ObjectId(file_id))
            sha256 = file_object.sha256
            vt = PublicApi(config.api_key)

            if 'upload' in request.POST:
                response = vt.scan_file(file_object.read(), filename=file_object.filename, from_disk=False)
                if response['results']['response_code'] == 1 and 'Scan request successfully queued' in response['results']['verbose_msg']:

                    return render(request, 'file_details_vt.html', {'state': 'pending',
                                                                    'vt_results': '',
                                                                    'file_id': file_id})
                else:
                    return render(request, 'file_details_vt.html', {'state': 'error',
                                                                    'vt_results': '',
                                                                    'file_id': file_id})
            else:

                response = vt.get_file_report(sha256)
                vt_fields = {}
                if response['results']['response_code'] == 1:
                    vt_fields['permalink'] = response['results']['permalink']
                    vt_fields['total'] = response['results']['total']
                    vt_fields['positives'] = response['results']['positives']
                    vt_fields['scandate'] = response['results']['scan_date']
                    vt_fields['scans'] = response['results']['scans']

                    # Store the results in datastore
                    store_data = {'file_id': ObjectId(file_id), 'vt': vt_fields}

                    db.create_datastore(store_data)
                    return render(request, 'file_details_vt.html', {'state': 'complete',
                                                                    'vt_results': vt_fields,
                                                                    'file_id': file_id})

                elif response['results']['response_code'] == -2:
                    # Still Pending Analysis
                    return render(request, 'file_details_vt.html', {'state': 'pending',
                                                                    'vt_results': vt_fields,
                                                                    'file_id': file_id})

                elif response['results']['response_code'] == 0:
                    # Not present in data set prompt to uploads
                    return render(request, 'file_details_vt.html', {'state': 'missing',
                                                                    'vt_results': vt_fields,
                                                                    'file_id': file_id})

    if command == 'yara-string':

        session_id = request.POST['session_id']

        if request.POST['yara-string'] != '':
            yara_string = request.POST['yara-string']
        else:
            yara_string = False

        if request.POST['yara-pid'] != '':
            yara_pid = request.POST['yara-pid']
        else:
            yara_pid = None

        if request.POST['yara-file'] != '':
            yara_file = os.path.join('yararules', request.POST['yara-file'])
        else:
            yara_file = None

        yara_hex = request.POST['yara-hex']
        if yara_hex != '':
            yara_hex = int(yara_hex)
        else:
            yara_hex = 256

        yara_reverse = request.POST['yara-reverse']
        if yara_reverse != '':
            yara_reverse = int(yara_reverse)
        else:
            yara_reverse = 0

        yara_case = request.POST['yara-case']
        if yara_case == 'true':
            yara_case = True
        else:
            yara_case = None

        yara_kernel = request.POST['yara-kernel']
        if yara_kernel == 'true':
            yara_kernel = True
        else:
            yara_kernel = None

        yara_wide = request.POST['yara-wide']
        if yara_wide == 'true':
            yara_wide = True
        else:
            yara_wide = None

        logger.debug('Yara String Scanner')

        try:
            session = db.get_session(ObjectId(session_id))
            vol_int = RunVol(session['session_profile'], session['session_path'])

            if yara_string:
                results = vol_int.run_plugin('yarascan', output_style='json', pid=yara_pid, plugin_options={
                                                                                          'YARA_RULES': yara_string,
                                                                                          'CASE': yara_case,
                                                                                          'ALL': yara_kernel,
                                                                                          'WIDE': yara_wide,
                                                                                          'SIZE': yara_hex,
                                                                                          'REVERSE': yara_reverse})

            elif yara_file:
                results = vol_int.run_plugin('yarascan', output_style='json', pid=yara_pid, plugin_options={
                                                                                          'YARA_FILE': yara_file,
                                                                                          'CASE': yara_case,
                                                                                          'ALL': yara_kernel,
                                                                                          'WIDE': yara_wide,
                                                                                          'SIZE': yara_hex,
                                                                                          'REVERSE': yara_reverse})
            else:
                return

            if 'Data' in results['columns']:
                row_loc = results['columns'].index('Data')

                for row in results['rows']:
                    try:
                        row[row_loc] = string_clean_hex(row[row_loc].decode('hex'))
                    except Exception as error:
                        logger.warning('Error converting hex to str: {0}'.format(error))

            return render(request, 'file_details_yara.html', {'yara': results, 'error': None})

        except Exception as error:
            logger.error(error)

    if command == 'yara':
        file_id = rule_file = False
        if 'file_id' in request.POST:
            file_id = request.POST['file_id']

        if 'rule_file' in request.POST:
            rule_file = request.POST['rule_file']

        if rule_file and file_id and YARA:
            file_object = db.get_filebyid(ObjectId(file_id))
            file_data = file_object.read()

            rule_file = os.path.join('yararules', rule_file)

            if os.path.exists(rule_file):
                rules = yara.compile(rule_file)
                matches = rules.match(data=file_data)
                results = {'rows': [], 'columns': ['Rule', 'process', 'Offset', 'Data']}
                for match in matches:
                    for item in match.strings:
                        results['rows'].append([match.rule, file_object.filename, item[0], string_clean_hex(item[2])])

            else:
                return render(request, 'file_details_yara.html', {'yara': None, 'error': 'Could not find Rule File'})

            if len(results) > 0:

                # Store the results in datastore
                store_data = {'file_id': ObjectId(file_id), 'yara': results}
                db.create_datastore(store_data)

            return render(request, 'file_details_yara.html', {'yara': results, 'error': None})

        else:
            return HttpResponse('Either No file ID or No Yara Rule was provided')

    if command == 'strings':
        if 'file_id' in request.POST:
            file_id = request.POST['file_id']
            file_object = db.get_filebyid(ObjectId(file_id))
            file_data = file_object.read()
            chars = " !\"#\$%&\'\(\)\*\+,-\./0123456789:;<=>\?@ABCDEFGHIJKLMNOPQRSTUVWXYZ\[\]\^_`abcdefghijklmnopqrstuvwxyz\{\|\}\\\~\t"
            shortest_run = 4
            regexp = '[%s]{%d,}' % (chars, shortest_run)
            pattern = re.compile(regexp)
            string_list_a = pattern.findall(file_data)
            regexp = b'((?:[%s]\x00){%d,})' % (chars, shortest_run)
            pattern = re.compile(regexp)
            string_list_u = [w.decode('utf-16').encode('ascii') for w in pattern.findall(file_data)]
            merged_list = string_list_a + string_list_u
            logger.debug('Joining Strings')
            string_list = '\n'.join(merged_list)

            '''
            String lists can get larger than the 16Mb bson limit
            Need to store in GridFS
            '''
            # Store the list in datastore
            store_data = {'file_id': ObjectId(file_id), 'string_list': string_list}
            logger.debug('Store Strings in DB')

            string_id = db.create_file(string_list, 'session_id', 'sha256', '{0}_strings.txt'.format(file_id))

            return HttpResponse('<td><a class="btn btn-success" role="button" href="/download/file/{0}">Download</a></td>'.format(string_id))

    if command == 'deleteobject':
        if 'droptype' in request.POST:
            drop_type = request.POST['droptype']

        if 'session_id' in request.POST:
            session_id = request.POST['session_id']

        if drop_type == 'session' and session_id:
            session_id = ObjectId(request.POST['session_id'])
            db.drop_session(session_id)
            return HttpResponse('OK')

        if 'file_id' in request.POST and drop_type == 'dumpfiles':

            plugin_id = request.POST['plugin_id']
            file_id = request.POST['file_id']
            plugin_details = db.get_pluginbyid(ObjectId(plugin_id))

            new_rows = []
            for row in plugin_details['plugin_output']['rows']:
                if str(file_id) in str(row):
                    pass
                else:
                    new_rows.append(row)
            plugin_details['plugin_output']['rows'] = new_rows

            # Drop file
            db.drop_file(ObjectId(file_id))

            # Update plugin
            db.update_plugin(ObjectId(plugin_id), plugin_details)

            return HttpResponse('OK')

    if command == 'memhex':
        if 'session_id' in request.POST:
            session_id = ObjectId(request.POST['session_id'])
            session = db.get_session(session_id)
            mem_path = session['session_path']
            if 'start_offset' and 'end_offset' in request.POST:
                try:
                    start_offset = int(request.POST['start_offset'], 0)
                    end_offset = int(request.POST['end_offset'], 0)
                    hex_cmd = 'hexdump -C -s {0} -n {1} {2}'.format(start_offset, end_offset - start_offset, mem_path)
                    hex_output = hex_dump(hex_cmd)
                    return HttpResponse(hex_output)
                except Exception as error:
                    return HttpResponse(error)

    if command == 'memhexdump':
        if 'session_id' in request.POST:
            session_id = ObjectId(request.POST['session_id'])
            session = db.get_session(session_id)
            mem_path = session['session_path']
            if 'start_offset' and 'end_offset' in request.POST:
                try:
                    start_offset = int(request.POST['start_offset'], 0)
                    end_offset = int(request.POST['end_offset'], 0)
                    mem_file = open(mem_path, 'rb')
                    # Get to start
                    mem_file.seek(start_offset)
                    file_data = mem_file.read(end_offset - start_offset)
                    response = HttpResponse(file_data, content_type='application/octet-stream')
                    response['Content-Disposition'] = 'attachment; filename="{0}-{1}.bin"'.format(start_offset,
                                                                                                  end_offset)
                    return response
                except Exception as error:
                    logger.error('Error Getting hex dump: {0}'.format(error))

    if command == 'addcomment':
        html_resp = ''
        if 'session_id' and 'comment_text' in request.POST:
            session_id = request.POST['session_id']
            comment_text = request.POST['comment_text']
            comment_data = {'session_id': ObjectId(session_id),
                            'comment_text': comment_text,
                            'date_added': datetime.now()}
            db.create_comment(comment_data)

            # now return all the comments for the ajax update

            for comment in db.get_commentbysession(ObjectId(session_id)):
                html_resp += '<pre>{0}</pre>'.format(comment['comment_text'])

        return HttpResponse(html_resp)

    if command == 'searchbar':
        if 'search_type' and 'search_text' and 'session_id' in request.POST:
            search_type = request.POST['search_type']
            search_text = request.POST['search_text']
            session_id = request.POST['session_id']

            logger.debug('{0} search for {1}'.format(search_type, search_text))

            if search_type == 'plugin':
                results = {'columns': ['Plugin Name', 'View Results'], 'rows': []}
                rows = db.search_plugins(search_text, session_id=ObjectId(session_id))
                for row in rows:
                    results['rows'].append([row['plugin_name'], '<a href="#" onclick="ajaxHandler(\'pluginresults\', \{{\'plugin_id\':\'{0}\'}}, false ); return false">View Output</a>'.format(row['_id'])])
                return render(request, 'plugin_output.html', {'plugin_results': results,
                                                              'bookmarks': [],
                                                              'plugin_id': 'None',
                                                              'plugin_name': 'Search Results',
                                                              'resultcount': len(results['rows'])})

            if search_type == 'hash':
                pass
            if search_type == 'string':
                logger.debug('yarascan for string')
                # If search string ends with .yar assume a yara rule
                if any(ext in search_text for ext in ['.yar', '.yara']):
                    if os.path.exists(search_text):
                        try:
                            session = db.get_session(ObjectId(session_id))
                            vol_int = RunVol(session['session_profile'], session['session_path'])
                            results = vol_int.run_plugin('yarascan', output_style='json',
                                                         plugin_options={'YARA_FILE': search_text})
                            return render(request, 'plugin_output_nohtml.html', {'plugin_results': results})
                        except Exception as error:
                            logger.error(error)
                    else:
                        logger.error('No Yara Rule Found')
                else:
                    try:
                        session = db.get_session(ObjectId(session_id))
                        vol_int = RunVol(session['session_profile'], session['session_path'])
                        results = vol_int.run_plugin('yarascan', output_style='json',
                                                     plugin_options={'YARA_RULES': search_text})
                        return render(request, 'plugin_output_nohtml.html', {'plugin_results': results})
                    except Exception as error:
                        logger.error(error)

            if search_type == 'registry':

                logger.debug('Registry Search')
                try:
                    session = db.get_session(ObjectId(session_id))
                    vol_int = RunVol(session['session_profile'], session['session_path'])
                    results = vol_int.run_plugin('printkey', output_style='json', plugin_options={'KEY': search_text})
                    return render(request, 'plugin_output.html', {'plugin_results': results,
                                                                  'bookmarks': [],
                                                                  'plugin_id': 'None',
                                                                  'plugin_name': 'Registry Search',
                                                                  'resultcount': len(results['rows'])})
                except Exception as error:
                    logger.error(error)

            if search_type == 'vol':
                # Run a vol command and get the output
                session = db.get_session(ObjectId(session_id))
                search_text = search_text.replace('%profile%', '--profile={0}'.format(session['session_profile']))
                search_text = search_text.replace('%path%', '-f {0}'.format(session['session_path']))

                vol_output = getoutput('vol.py {0}'.format(search_text))

                results = {'rows': [['<pre>{0}</pre>'.format(vol_output)]], 'columns': ['Volatility Raw Output']}

                # Consider storing the output here as well.

                return render(request, 'plugin_output.html', {'plugin_results': results,
                                                              'bookmarks': [],
                                                              'plugin_id': 'None',
                                                              'plugin_name': 'Volatility Command Line',
                                                              'resultcount': len(results['rows'])})

            return HttpResponse('No valid search query found.')

    if command == 'pluginresults':
        if 'start' in request.POST:
            start = int(request.POST['start'])
        else:
            start = 0

        if 'length' in request.POST:
            length = int(request.POST['length'])
        else:
            length = 25

        if 'plugin_id' in request.POST:
            plugin_id = ObjectId(request.POST['plugin_id'])
            plugin_id = ObjectId(plugin_id)
            plugin_results = db.get_pluginbyid(plugin_id)
            output = plugin_results['plugin_output']['rows']
            resultcount = len(plugin_results['plugin_output']['rows'])

            # Get Bookmarks
            try:
                bookmarks = db.get_pluginbyid(plugin_id)['bookmarks']
            except:
                bookmarks = []

        else:
            return JsonResponse({'error': 'No Plugin ID'})

        # If we are paging with datatables
        if 'pagination' in request.POST:
            paged_data = []

            # Searching
            if 'search[value]' in request.POST:
                search_term = request.POST['search[value]']
                # output = [r for r in output if search_term.lower() in r]
                output = filter(lambda x: search_term.lower() in str(x).lower(), output)
            else:
                output = []

            # Column Sort
            col_index = int(request.POST['order[0][column]'])
            if request.POST['order[0][dir]'] == 'asc':
                direction = False
            else:
                direction = True

            # Test column data for correct sort
            try:
                output = sorted(output, key=lambda x: int(x[col_index]), reverse=direction)
            except:
                output = sorted(output, key=lambda x: str(x[col_index]).lower(), reverse=direction)

            # Get number of Rows
            for row in output[start:start+length]:
                paged_data.append(row)

            datatables = {
                "draw": request.POST['draw'],
                "recordsTotal": resultcount,
                "recordsFiltered": len(output),
                "data": paged_data
            }

            return JsonResponse(datatables)

        # Else return standard 25 rows
        else:
            plugin_results['plugin_output']['rows'] = plugin_results['plugin_output']['rows'][start:length]

            return render(request, 'plugin_output.html', {'plugin_results': plugin_results['plugin_output'],
                                                          'plugin_id': plugin_id,
                                                          'bookmarks': bookmarks,
                                                          'resultcount': resultcount,
                                                          'plugin_name': plugin_results['plugin_name']})

    if command == 'bookmark':
        if 'row_id' in request.POST:
            plugin_id, row_id = request.POST['row_id'].split('_')
            plugin_id = ObjectId(plugin_id)
            row_id = int(row_id)
            # Get Bookmarks for plugin
            try:
                bookmarks = db.get_pluginbyid(plugin_id)['bookmarks']
            except:
                bookmarks = []
            # Update bookmarks
            if row_id in bookmarks:
                bookmarks.remove(row_id)
                bookmarked = 'remove'
            else:
                bookmarks.append(row_id)
                bookmarked = 'add'

            # Update Plugins
            new_values = {'bookmarks': bookmarks}
            db.update_plugin(ObjectId(plugin_id), new_values)
            return HttpResponse(bookmarked)

    if command == 'procmem':
        if 'row_id' in request.POST and 'session_id' in request.POST:
            plugin_id, row_id = request.POST['row_id'].split('_')
            session_id = request.POST['session_id']
            plugin_id = ObjectId(plugin_id)
            row_id = int(row_id)
            plugin_data = db.get_pluginbyid(ObjectId(plugin_id))['plugin_output']
            row = plugin_data['rows'][row_id - 1]
            pid = row[3]

            plugin_row = db.get_plugin_byname('memdump', ObjectId(session_id))

            logger.debug('Running Plugin: memdump with pid {0}'.format(pid))

            res = run_plugin(session_id, plugin_row['_id'], pid=pid)
            return HttpResponse(res)

    if command == 'filedump':
        if 'row_id' in request.POST and 'session_id' in request.POST:
            plugin_id, row_id = request.POST['row_id'].split('_')
            session_id = request.POST['session_id']
            plugin_id = ObjectId(plugin_id)
            row_id = int(row_id)
            plugin_data = db.get_pluginbyid(ObjectId(plugin_id))['plugin_output']
            row = plugin_data['rows'][row_id - 1]
            offset = row[1]

            plugin_row = db.get_plugin_byname('dumpfiles', ObjectId(session_id))

            logger.debug('Running Plugin: dumpfiles with offset {0}'.format(offset))

            res = run_plugin(session_id, plugin_row['_id'], plugin_options={'PHYSOFFSET': str(offset),
                                                                            'NAME': True,
                                                                            'REGEX': None})
            return HttpResponse(res)

    return HttpResponse('No valid search query found.')
