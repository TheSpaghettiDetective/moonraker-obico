import sys
import json
import os
import distro
import subprocess
import re
import shutil


from .utils import is_os_64bit

JANUS_ROOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'janus')
RUNTIME_JANUS_ETC_DIR = os.path.join(JANUS_ROOT_DIR, 'runtime', 'etc', 'janus')
TPL_JANUS_ETC_DIR = os.path.join(JANUS_ROOT_DIR, 'templates', 'etc', 'janus')
PRECOMPILED_DIR = '{root_dir}/precomplied/{os_id}.{os_version}.{os_bit}-bit'.format(root_dir=JANUS_ROOT_DIR, os_id=distro.id(), os_version=distro.major_version(), os_bit='64' if is_os_64bit() else '32')

def precompiled_janus_jcfg_folders_section(precompiled_janus_dir):
  lib_dir = os.path.join(precompiled_janus_dir, 'lib')
  if os.path.exists(lib_path) and os.path.isdir(lib_path):
      return """
            plugins_folder = "{lib_dir}/janus/plugins"                     # Plugins folder
            transports_folder = "{lib_dir}/janus/transports"       # Transports folder
            events_folder = "{lib_dir}/janus/events"                       # Event handlers folder
            loggers_folder = "{lib_dir}/janus/loggers"
    """.format(lib_dir=lib_dir)

  return None

def system_janus_jcfg_folders_section(janus_jcfg_path):
  pattern = r'^\s*(plugins_folder|transports_folder|events_folder|loggers_folder)\s*='
  filtered_lines = []

  with open(janus_jcfg_path, 'r') as f:
      for line in f:
          if re.search(pattern, line):
              filtered_lines.append(line)

  return ''.join(filtered_lines)


def find_system_janus_jcfg_path():
    janus_path = None
    janus_jcfg_path = None
    try:
        output = subprocess.check_output(['dpkg', '-L', 'janus'], universal_newlines=True)
        paths = output.split('\n')
        for path in paths:
            path = path.strip()
            if path.endswith('/janus.jcfg'):
                janus_jcfg_path = path
            if path.endswith('/janus') and os.path.isfile(path) and os.access(path, os.X_OK):
                janus_path = path
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    return (janus_path, janus_jcfg_path)


def build_janus_jcfg(auth_token):
    janus_jcfg_path = "{etc_dir}/janus.jcfg".format(etc_dir=RUNTIME_JANUS_ETC_DIR)

    (system_janus_bin_path, system_janus_jcfg_path) = find_system_janus_jcfg_path()
    if system_janus_bin_path and system_janus_jcfg_path:
        folder_section = system_janus_jcfg_folders_section(system_janus_jcfg_path)
    else:
        folder_section = precompiled_janus_jcfg_folders_section(PRECOMPILED_DIR)

    if not folder_section:
        return False

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

def streaming_jcfg_rtsp_section(janus_section_id, rtsp_url, dataport):

    return("""
h264-{janus_section_id}: {{
        type = "rtsp"
        id = {janus_section_id}
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
""".format(janus_section_id=janus_section_id, rtsp_url=rtsp_url, dataport=dataport))


def streaming_jcfg_rtp_section(janus_section_id, videoport, videortcpport, dataport):
    return("""
h264-{janus_section_id}: {{
        type = "rtp"
        id = {janus_section_id}
        description = "h264-video"
        enabled = true
        audio = false
        audioiface = "127.0.0.1"
        enabled = true
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
""".format(janus_section_id=janus_section_id, videoport=videoport, videortcpport=videortcpport, dataport=dataport))


def streaming_jcfg_mjpeg_section(janus_section_id, mjpeg_dataport):
    return("""
mjpeg-{janus_section_id}: {{
        type = "rtp"
        id = {janus_section_id}
        description = "mjpeg-data"
        audio = false
        video = false
        data = true
        dataport = {mjpeg_dataport}
        datatype = "binary"
        dataiface = "127.0.0.1"
        databuffermsg = false
}}
""".format(janus_section_id=janus_section_id, mjpeg_dataport=mjpeg_dataport))


def build_janus_plugin_streaming_jcfg(webcams):
    streaming_jcfg_path = '{etc_dir}/janus.plugin.streaming.jcfg'.format(etc_dir=RUNTIME_JANUS_ETC_DIR)
    with open(streaming_jcfg_path, 'w') as f:
        for webcam in webcams:
            if webcam['moonraker_config']['service'] == 'webrtc-camerastreamer' and webcam['config'].get('rtsp_port'):
                f.write(streaming_jcfg_rtsp_section(webcam['runtime']['janus_section_id'], 'rtsp://127.0.0.1:{rtsp_port}/stream.h264'.format(rtsp_port=webcam['config']['rtsp_port']), webcam['runtime']['dataport']))
            elif 'mjpeg' in webcam['moonraker_config']['service']:
                if webcam['runtime'].get('mjpeg_dataport'):
                    f.write(streaming_jcfg_mjpeg_section(webcam['runtime']['janus_section_id'], webcam['runtime']['mjpeg_dataport']))
                elif webcam['runtime'].get('videoport') and webcam['runtime'].get('videortcpport') and webcam['runtime'].get('dataport'):
                    f.write(streaming_jcfg_rtp_section(webcam['runtime']['janus_section_id'], webcam['runtime']['videoport'], webcam['runtime']['videortcpport'], webcam['runtime']['dataport']))
                else:
                    raise Exception('Got webcam config {webcam} missing info for janus config'.format(webcam=webcam))
            else:
                webcam['stream_error'] = 'Got webcam config {webcam} not suitable for streaming'.format(webcam=webcam)


def build_janus_transport_websocket_jcfg():
    target_path = "{etc_dir}/janus.transport.websockets.jcfg".format(etc_dir=RUNTIME_JANUS_ETC_DIR)
    tpl_path = "{tpl_etc_dir}/janus.transport.websockets.jcfg.template".format(tpl_etc_dir=TPL_JANUS_ETC_DIR)
    shutil.copy(tpl_path, target_path)

def build_janus_config(webcams, printer_auth_token):
    build_janus_jcfg(printer_auth_token)
    build_janus_plugin_streaming_jcfg(webcams)
    build_janus_transport_websocket_jcfg()


if __name__ == '__main__':
    file_path = sys.argv[1]
    with open(file_path, 'r') as json_file:
        webcams = json.load(json_file)['result']['webcams']

    build(webcams)
