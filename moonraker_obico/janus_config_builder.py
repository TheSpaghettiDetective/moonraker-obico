import sys
import logging
import json
import os
import distro
import subprocess
import re

from .utils import os_bit, pi_version, board_id

JANUS_ROOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'janus')
RUNTIME_JANUS_ETC_DIR = os.path.join(JANUS_ROOT_DIR, 'runtime', 'etc', 'janus')
TPL_JANUS_ETC_DIR = os.path.join(JANUS_ROOT_DIR, 'templates', 'etc', 'janus')

distro_id = distro.id()
if distro_id == 'raspbian' and pi_version(): # On some Raspbian/RPi OS versions, distro.id() returns 'debian'. On others, it returns 'raspbian'.
    distro_id = 'debian'

PRECOMPILED_DIR = '{root_dir}/precomplied/{board_id}.{os_id}.{os_version}.{os_bit}'.format(root_dir=JANUS_ROOT_DIR, board_id=board_id(), os_id=distro_id, os_version=distro.major_version(), os_bit=os_bit())

_logger = logging.getLogger('obico.janus_config_builder')

def janus_jcfg_folders_section(lib_dir):
    return """
            plugins_folder = "{lib_dir}/janus/plugins"                     # Plugins folder
            transports_folder = "{lib_dir}/janus/transports"       # Transports folder
            events_folder = "{lib_dir}/janus/events"                       # Event handlers folder
            loggers_folder = "{lib_dir}/janus/loggers"
""".format(lib_dir=lib_dir)

def find_system_janus_paths():
    janus_path = None
    janus_lib_path = None

    try:
        output = subprocess.check_output(['dpkg', '-L', 'janus'], universal_newlines=True)
        paths = output.split('\n')

        # janus binary path if only 1 line ends with /bin/janus
        janus_paths = [path.strip() for path in paths if path.strip().endswith('/bin/janus')]
        if len(janus_paths) == 1:
            janus_path = janus_paths[0]

        # janus lib path if line contains plugins/libjanus_streaming.so
        janus_lib_paths = [path.strip() for path in paths if '/janus/plugins/libjanus_streaming.so' in path]
        if janus_lib_paths:
            janus_lib_path = os.path.dirname(janus_lib_paths[0])
            janus_lib_path = os.path.normpath(janus_lib_path)
            if janus_lib_path.endswith('/janus/plugins'):
                janus_lib_path = janus_lib_path[:-14]  # remove "/plugins" from the end

    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    return (janus_path, janus_lib_path)


def build_janus_jcfg(auth_token):
    janus_jcfg_path = "{etc_dir}/janus.jcfg".format(etc_dir=RUNTIME_JANUS_ETC_DIR)

    ld_lib_path = None
    janus_bin_path = None
    folder_section = None

    (janus_bin_path, system_janus_lib_path) = find_system_janus_paths()
    if janus_bin_path and system_janus_lib_path:
        folder_section = janus_jcfg_folders_section(system_janus_lib_path)
    else:
        janus_bin_path = os.path.join(PRECOMPILED_DIR, 'bin', 'janus')
        ld_lib_path = os.path.join(PRECOMPILED_DIR, 'lib')
        if os.path.exists(janus_bin_path) and os.path.exists(ld_lib_path) and os.path.isdir(ld_lib_path):
            folder_section = janus_jcfg_folders_section(ld_lib_path)

    if not janus_bin_path or not folder_section:
        return (None, None)

    with open(janus_jcfg_path, 'w') as f:
        f.write("""
general: {
""")

        f.write(folder_section)

        f.write("""
        admin_secret = "janusoverlord"  # String that all Janus requests must contain
}}
nat: {{
        turn_server = "turn.obico.io"
        turn_port = 80
        turn_type = "tcp"
        turn_user = "{auth_token}"
        turn_pwd = "{auth_token}"
""".format(auth_token=auth_token))

        f.write("""
        ice_ignore_list = "vmnet"
        ignore_unreachable_ice_server = true
}
plugins: {
        disable = "libjanus_audiobridge.so,libjanus_echotest.so,libjanus_nosip.so,libjanus_sip.so,libjanus_textroom.so,libjanus_videoroom.so,libjanus_duktape.so,libjanus_lua.so,libjanus_recordplay.so,libjanus_videocall.so,libjanus_voicemail.so"
}
transports: {
        disable = "libjanus_mqtt.so,libjanus_nanomsg.so,libjanus_pfunix.so,libjanus_rabbitmq.so,libjanus_http.so"
}
events: {
}
""")

    return (janus_bin_path, ld_lib_path)

def streaming_jcfg_rtsp_section(stream_id, rtsp_url, dataport):

    return("""
h264-{stream_id}: {{
        type = "rtsp"
        id = {stream_id}
        description = "h264-video"
        enabled = true
        audio = false
        audioiface = "127.0.0.1"
        video = true
        url = "{rtsp_url}"
        videopt = 96
        videortpmap = "H264/90000"
        videofmtp = "profile-level-id=42e01f;packetization-mode=1"
        data = true
        dataport = {dataport}
        datatype = "binary"
        dataiface = "127.0.0.1"
        databuffermsg = false
}}
""".format(stream_id=stream_id, rtsp_url=rtsp_url, dataport=dataport))


def streaming_jcfg_rtp_section(stream_id, videoport, videortcpport, dataport):
    return("""
h264-{stream_id}: {{
        type = "rtp"
        id = {stream_id}
        description = "h264-video"
        enabled = true
        audio = false
        audioiface = "127.0.0.1"
        video = true
        videoport =  {videoport}
        videortcpport = {videortcpport}
        videoiface = "127.0.0.1"
        videopt = 96
        videortpmap = "H264/90000"
        videofmtp = "profile-level-id=42e01f;packetization-mode=1"
        data = true
        dataport = {dataport}
        datatype = "binary"
        dataiface = "127.0.0.1"
        databuffermsg = false
}}
""".format(stream_id=stream_id, videoport=videoport, videortcpport=videortcpport, dataport=dataport))

def streaming_jcfg_data_channel_only_section(stream_id, dataport):
    return("""
data-{stream_id}: {{
        type = "rtp"
        id = {stream_id}
        description = "data"
        enabled = true
        audio = false
        video = false
        data = true
        dataport = {dataport}
        datatype = "binary"
        dataiface = "127.0.0.1"
        databuffermsg = false
}}
""".format(stream_id=stream_id, dataport=dataport))

def streaming_jcfg_mjpeg_section(stream_id, mjpeg_dataport):
    return("""
mjpeg-{stream_id}: {{
        type = "rtp"
        id = {stream_id}
        description = "mjpeg-data"
        audio = false
        video = false
        data = true
        dataport = {mjpeg_dataport}
        datatype = "binary"
        dataiface = "127.0.0.1"
        databuffermsg = false
}}
""".format(stream_id=stream_id, mjpeg_dataport=mjpeg_dataport))


def build_janus_plugin_streaming_jcfg(webcams):
    streaming_jcfg_path = '{etc_dir}/janus.plugin.streaming.jcfg'.format(etc_dir=RUNTIME_JANUS_ETC_DIR)
    with open(streaming_jcfg_path, 'w') as f:
        for webcam in webcams:
            if webcam.streaming_params['mode'] == 'h264_rtsp':
                if webcam.streaming_params.get('rtsp_port'):
                    f.write(streaming_jcfg_rtsp_section(webcam.runtime['stream_id'], 'rtsp://127.0.0.1:{rtsp_port}/stream.h264'.format(rtsp_port=webcam.streaming_params['rtsp_port']), webcam.runtime['dataport']))
                else:
                    raise Exception('streaming_params.rtsp_port is required to do h264_rtsp streaming')

            elif webcam.streaming_params['mode'] in ('h264_copy', 'h264_transcode', 'h264_device'):
                if webcam.runtime.get('videoport') and webcam.runtime.get('videortcpport') and webcam.runtime.get('dataport'):
                    f.write(streaming_jcfg_rtp_section(webcam.runtime['stream_id'], webcam.runtime['videoport'], webcam.runtime['videortcpport'], webcam.runtime['dataport']))
                else:
                    raise Exception('Missing runtime parameters required in building h264-xxx section')

            elif webcam.streaming_params['mode'] == 'mjpeg_webrtc':
                if webcam.runtime.get('mjpeg_dataport'):
                    f.write(streaming_jcfg_mjpeg_section(webcam.runtime['stream_id'], webcam.runtime['mjpeg_dataport']))
                else:
                    raise Exception('Missing runtime parameters required in building mjpeg_webrtc section')

            elif webcam.streaming_params['mode'] == 'data_channel_only':
                if webcam.runtime.get('dataport'):
                    f.write(streaming_jcfg_data_channel_only_section(webcam.runtime['stream_id'], webcam.runtime['dataport']))
                else:
                    raise Exception('Missing runtime parameters required in building streaming_jcfg_data_channel_only_section section')

            else:
                raise Exception('Unknown streaming mode "{}"'.format(webcam.streaming_params['mode']))


def build_janus_transport_websocket_jcfg(ws_port, admin_ws_port):
    target_path = "{etc_dir}/janus.transport.websockets.jcfg".format(etc_dir=RUNTIME_JANUS_ETC_DIR)
    with open(target_path, 'w') as f:
        f.write("""
# WebSockets stuff: whether they should be enabled, which ports they
# should use, and so on.
general: {{
	json = "indented"				# Whether the JSON messages should be indented (default),
									# plain (no indentation) or compact (no indentation and no spaces)
	#pingpong_trigger = 30			# After how many seconds of idle, a PING should be sent
	#pingpong_timeout = 10			# After how many seconds of not getting a PONG, a timeout should be detected

	ws = true						# Whether to enable the WebSockets API
	ws_port = {ws_port}				# WebSockets server port
	#ws_interface = "eth0"			# Whether we should bind this server to a specific interface only
	ws_ip = "127.0.0.1"			# Whether we should bind this server to a specific IP address only
	wss = false						# Whether to enable secure WebSockets
	#wss_port = 8989				# WebSockets server secure port, if enabled
	#wss_interface = "eth0"			# Whether we should bind this server to a specific interface only
	#wss_ip = "192.168.0.1"			# Whether we should bind this server to a specific IP address only
	#ws_logging = "err,warn"		# libwebsockets debugging level as a comma separated list of things
									# to debug, supported values: err, warn, notice, info, debug, parser,
									# header, ext, client, latency, user, count (plus 'none' and 'all')
	#ws_acl = "127.,192.168.0."		# Only allow requests coming from this comma separated list of addresses
}}

# If you want to expose the Admin API via WebSockets as well, you need to
# specify a different server instance, as you cannot mix Janus API and
# Admin API messaging. Notice that by default the Admin API support via
# WebSockets is disabled.
admin: {{
	admin_ws = false					# Whether to enable the Admin API WebSockets API
	admin_ws_port = {admin_ws_port}		# Admin API WebSockets server port, if enabled
	#admin_ws_interface = "eth0"		# Whether we should bind this server to a specific interface only
	#admin_ws_ip = "192.168.0.1"		# Whether we should bind this server to a specific IP address only
	admin_wss = false					# Whether to enable the Admin API secure WebSockets
	#admin_wss_port = 7989				# Admin API WebSockets server secure port, if enabled
	#admin_wss_interface = "eth0"		# Whether we should bind this server to a specific interface only
	#admin_wss_ip = "192.168.0.1"		# Whether we should bind this server to a specific IP address only
	#admin_ws_acl = "127.,192.168.0."	# Only allow requests coming from this comma separated list of addresses
}}

# Certificate and key to use for any secure WebSocket server, if enabled (and passphrase if needed).
certificates: {{
	#cert_pem = "/path/to/cert.pem"
	#cert_key = "/path/to/key.pem"
	#cert_pwd = "secretpassphrase"
}}
""".format(ws_port=ws_port, admin_ws_port=admin_ws_port))


def build_janus_config(webcams, printer_auth_token, ws_port, admin_ws_port):
    if not os.path.exists(RUNTIME_JANUS_ETC_DIR):
        os.makedirs(RUNTIME_JANUS_ETC_DIR)

    (janus_bin_path, ld_lib_path) = build_janus_jcfg(printer_auth_token)
    _logger.info('janus_bin_path: {janus_bin_path} - ld_lib_path: {ld_lib_path}'.format(janus_bin_path=janus_bin_path, ld_lib_path=ld_lib_path))
    build_janus_plugin_streaming_jcfg(webcams)
    build_janus_transport_websocket_jcfg(ws_port, admin_ws_port)

    return (janus_bin_path, ld_lib_path)
