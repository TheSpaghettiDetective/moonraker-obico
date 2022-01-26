Standalone application for TheSpaghettiDetective - moonraker integration
------------------------------------------------------------------------

Alpha version, with limited functionality and for development only.

Please don't use it in production and be careful while using it!


Preparing Klipper and Moonraker
-------------------------------

Following sections are mandatory in printer.cfg (of Klipper):

```

[virtual_sdcard]
path: <path to existing dir>

[display_status]

[pause_resume]

```

Following sections are mandatory in Moonraker's config file:

```

[history]

[update_manager tsd-moonraker]
type: git_repo
path: ~/tsd-moonraker
origin: https://github.com/TheSpaghettiDetective/tsd-moonraker.git
primary_branch: main
env: ~/tsd-moonraker/bin/python3
requirements: requirements.txt
install_script: install.sh
is_system_service: True


```

Grab moonraker api key from ```/access/api_key```.
If TSD agent connects from a [trusted host](https://moonraker.readthedocs.io/en/latest/configuration/#authorization), you can skip this.


config.ini
----------

```
[thespaghettidetective]
url = <by default it connects to TSD Cloud>
auth_token = <filled in by link command, see bellow>

[moonraker]
url = <url for moonraker api, default is http://127.0.0.1:7125>
api_key = <grab it from moonraker, visit /access/api_key from trusted host>

[webcam]
snapshot_url = <defaults to http://127.0.0.1:8080/?action=snapshot>
# or
# stream_url = http://127.0.0.1:8080/?action=stream
```


How to run
----------

    # go to home directory
    
    cd ~
    
    # clone repo

    git clone https://github.com/TheSpaghettiDetective/tsd-moonraker.git
    
    # when the process is done, run the install script:
    
    cd tsd-moonraker
    chmod +x install.sh
    ./install.sh

    # fill in essential configuration

    nano ~/klipper_config/config.ini

    # link printer (grab tsd auth token)

    python3 -m tsd_moonraker.link -c ~/klipper_config/config.ini

    # start service

    sudo systemctl start tsd-moonraker
