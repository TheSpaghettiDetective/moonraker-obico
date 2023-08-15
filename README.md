# Obico for Klipper

This is a Moonraker plugin that enables the Klipper-based 3D printers to connect to Obico.

[Obico](https://www.obico.io) is a community-built, open-source smart 3D printing platform used by makers, enthusiasts, and tinkerers around the world.


# Installation

    cd ~
    git clone https://github.com/TheSpaghettiDetective/moonraker-obico.git
    cd moonraker-obico
    ./install.sh

[Detailed documentation](https://obico.io/docs/user-guides/klipper-setup/).


# Uninstall

    sudo systemctl stop moonraker-obico.service
    sudo systemctl disable moonraker-obico.service
    sudo rm /etc/systemd/system/moonraker-obico.service
    sudo systemctl daemon-reload
    sudo systemctl reset-failed
    rm -rf ~/moonraker-obico
    rm -rf ~/moonraker-obico-env


# Use the docker Image

## Link your printer

1. Create a copy of `moonraker-obico.cfg.sample` in a directory of your choice. 

```bash
cp moonraker-obico.cfg.sample /opt/mydir/moonraker-obico.cfg
```

2. Set the `[moonraker].host` and `[moonraker].port` in the newly created config file, according to the [Documentation](https://www.obico.io/docs/user-guides/moonraker-obico/config/), making sure to **not** set the `[server].auth_token`.

3. Open the Obico Webinterface or App to obtain the *6-digit verification code* for a `Klipper`-Type Printer.

4. Replace `/opt/mydir/moonraker-obico.cfg` in the following command with the path to your configuration file and run it. Enter the Code obtained in the previous step, when promted.

```bash
docker run --rm -it \
  -v /opt/mydir/moonraker-obico.cfg:/opt/printer_data/config/moonraker-obico.cfg \
  --entrypoint /opt/venv/bin/python \
  ghcr.io/thespaghettidetective/moonraker-obico:latest \
    -m moonraker_obico.link -c /opt/printer_data/config/moonraker-obico.cfg
```

5. Check that your configuration file now contains a value for `[server].auth_token`

## Run the application

Given, that your moonraker-obico.cfg now contains a valid `[server].auth_token`, a container may be started using the following command:

```bash
docker run -d \
  --name moonraker-obico \
  -v /opt/mydir/moonraker-obico.cfg:/opt/printer_data/config/moonraker-obico.cfg \
  ghcr.io/thespaghettidetective/moonraker-obico:latest
```


# Set up a dev environment

    cd ~
    git clone https://github.com/TheSpaghettiDetective/moonraker-obico.git
    cd moonraker-obico
    virtualenv -p /usr/bin/python3 --system-site-packages ~/moonraker-obico-env
    source ~/moonraker-obico-env/bin/activate
    pip3 install -r requirements.txt

    # fill in essential configuration
    cp moonraker-obico.cfg.sample moonraker-obico.cfg

    # link printer (grab Obico auth token)
    python3 -m moonraker_obico.link -c moonraker-obico.cfg

    # start app
    python3 -m moonraker_obico.app -c moonraker-obico.cfg
