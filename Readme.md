TheSpaghettiDetective - Moonraker plugin
----------------------------------------

Alpha version, with limited functionality and for development only.

Please don't use it in production and be careful while using it!


Klipper Configuration Requirements
----------------------------------

Following sections are mandatory in printer.cfg (of Klipper):

```
[virtual_sdcard]
path: <path to existing dir>

[display_status]

[pause_resume]
```

Moonraker Configuration Requirements
------------------------------------

Following sections are mandatory in Moonraker's config file:

```
[history]
```

Configuration
-------------

Create plugin's config.ini with following content:

```
[thespaghettidetective]
url = <by default it connects to TSD Cloud, optional>
auth_token = <filled in by link command, see bellow>

[moonraker]
url = <url for moonraker api, default is http://127.0.0.1:7125>
api_key = <grab it from moonraker, visit /access/api_key from trusted host>

[webcam]
snapshot_url = <defaults to http://127.0.0.1:8080/?action=snapshot>
# or
# stream_url = http://127.0.0.1:8080/?action=stream
```

You can grab Moonraker api key from ```/access/api_key```.
If TSD plugin connects from a [trusted host](https://moonraker.readthedocs.io/en/latest/configuration/#authorization), you can skip this.

How to install (systemd)
------------------------

    # clone repo
    
    cd ~
    git clone https://github.com/TheSpaghettiDetective/tsd-moonraker.git
    
    # when the process is done, run the install script:
    
    cd tsd-moonraker
    ./scripts/install.sh
    
    # fill in essential configuration
    
    nano ~/klipper_config/config.ini
    
    # link printer (grab tsd auth token)
    source ~/tsd-moonraker-env/bin/activate
    python3 -m tsd_moonraker.link -c ~/klipper_config/config.ini
    
    # start service
    
    sudo systemctl start tsd-moonraker
    

Add an entry for Moonraker's update manager:

```
[update_manager tsd-moonraker]
type: git_repo
path: ~/tsd-moonraker
origin: https://github.com/TheSpaghettiDetective/tsd-moonraker.git
primary_branch: main
env: ~/tsd-moonraker-env/bin/python3
requirements: requirements.txt
install_script: scripts/install.sh
is_system_service: True
```

How to run without installation
-------------------------------

    # requires python3; install python3 packages

    sudo apt-get install python3 python3-pip python3-venv

    # clone repo

    git clone https://github.com/TheSpaghettiDetective/tsd-moonraker.git

    # setup virtual environment

    python3 -m venv tsd-moonraker/
    cd tsd-moonraker
    source ./bin/activate
    pip3 install -r requirements.txt

    # fill in essential configuration

    cp config.sample.ini config.ini
    nano config.ini

    # link printer (grab tsd auth token)

    python3 -m tsd_moonraker.link -c config.ini

    # start app

    python3 -m tsd_moonraker.app -c config.ini -l tsd_moonraker.log
