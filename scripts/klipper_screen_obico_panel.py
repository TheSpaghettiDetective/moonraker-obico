import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GdkPixbuf, Pango
from ks_includes.screen_panel import ScreenPanel
import qrcode
from io import BytesIO
import logging
import requests

OBICO_LINK_STATUS_MACRO = 'OBICO_LINK_STATUS'

class Panel(ScreenPanel):
    def __init__(self, screen, title):
        super().__init__(screen, title)

        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.content.add(container)

        gcode_macros = self._printer.get_gcode_macros()
        gcode_macros_lower = [macro.lower() for macro in gcode_macros]

        if OBICO_LINK_STATUS_MACRO not in [macro.upper() for macro in gcode_macros]:
            self.display_setup_guide_qr_code(container)

        else:
            moonraker_config = self.get_connected_moonraker_config(self, self._screen)
            moonraker_host = moonraker_config.get('moonraker_host', '127.0.0.1')
            moonraker_port = moonraker_config.get('moonraker_port', 7125)

            url = f'http://{moonraker_host}:{moonraker_port}/printer/objects/query?gcode_macro%20{OBICO_LINK_STATUS_MACRO}'

            response = requests.get(url)
            response.raise_for_status().json()  # This will raise an exception for HTTP errors
            logging.info(data)

            is_linked = data.get('result', {}).get('status', {}).get('gcode_macro OBICO_LINK_STATUS', {}).get('is_linked')
            one_time_passcode = data.get('result', {}).get('status', {}).get('gcode_macro OBICO_LINK_STATUS', {}).get('one_time_passcode')

            if is_linked is None:
                self.display_setup_guide_qr_code(container)
            elif is_linked:
                self.display_linked_status(container)
            elif one_time_passcode:
                self.display_link_qr_code(container)
            else:
                self.display_setup_guide_qr_code(container)

        # Update the panel's display
        self.content.show_all()

    def display_linked_status(self, container):
        guide_text = "Printer is linked to Obico server."
        guide_label = Gtk.Label()
        guide_label.set_markup(f"<big><b>{guide_text}</b></big>")
        guide_label.set_hexpand(True)
        guide_label.set_vexpand(True)
        guide_label.set_halign(Gtk.Align.START)
        guide_label.set_valign(Gtk.Align.CENTER)
        guide_label.set_line_wrap(True)  # Enable line wrapping
        guide_label.set_line_wrap_mode(Pango.WrapMode.WORD)  # Break lines at word boundaries
        guide_label.set_margin_end(10)  # Add 10 pixels of margin on the right
        guide_label.set_margin_bottom(10)  # Add 10 pixels of margin on the bottom
        container.pack_start(guide_label, True, False, 0)

    def display_link_qr_code(self, container):
            # Generate a QR code
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=4,
            )
            qr.add_data('https://google.com')
            qr.make(fit=True)

            img = qr.make_image(fill_color="black", back_color="white")
            img_byte_arr = BytesIO()
            img.save(img_byte_arr, format='PNG')
            img_byte_arr = img_byte_arr.getvalue()

            # Load the QR code image into a GdkPixbuf
            loader = GdkPixbuf.PixbufLoader.new_with_type('png')
            loader.write(img_byte_arr)
            loader.close()
            pixbuf = loader.get_pixbuf()

            # Create an image widget to display the QR code
            qr_image = Gtk.Image.new_from_pixbuf(pixbuf)
            container.pack_start(qr_image, True, False, 0)

            # Create a label with "test text"
            label = Gtk.Label(label="test text")
            container.pack_start(label, True, False, 0)

    def get_connected_moonraker_config(self, _screen):
        connected_printer_name = _screen.connected_printer
        connected_printer_dict = {}

        for printer in _screen._config.get_printers():
            if connected_printer_name in printer.keys():
                connected_printer_dict = printer[connected_printer_name]
                break
        return connected_printer_dict

    def display_setup_guide_qr_code(self, container):
        # Create a label with the guide text
        guide_text = "Obico is state-of-the-art AI for 3D printing. It also provides a mobile app for free. Scan this QR code to set it up."
        guide_label = Gtk.Label()
        guide_label.set_markup(f"<big><b>{guide_text}</b></big>")
        guide_label.set_hexpand(True)
        guide_label.set_vexpand(True)
        guide_label.set_halign(Gtk.Align.START)
        guide_label.set_valign(Gtk.Align.CENTER)
        guide_label.set_line_wrap(True)  # Enable line wrapping
        guide_label.set_line_wrap_mode(Pango.WrapMode.WORD)  # Break lines at word boundaries
        guide_label.set_margin_end(10)  # Add 10 pixels of margin on the right
        guide_label.set_margin_bottom(10)  # Add 10 pixels of margin on the bottom
        container.pack_start(guide_label, True, False, 0)

        # Generate a QR code
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data('https://obico.io/docs/user-guides/klipper-screen-setup/')
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")
        img_byte_arr = BytesIO()
        img.save(img_byte_arr, format='PNG')
        img_byte_arr = img_byte_arr.getvalue()

        # Load the QR code image into a GdkPixbuf
        loader = GdkPixbuf.PixbufLoader.new_with_type('png')
        loader.write(img_byte_arr)
        loader.close()
        pixbuf = loader.get_pixbuf()

        # Create an image widget to display the QR code
        qr_image = Gtk.Image.new_from_pixbuf(pixbuf)
        container.pack_start(qr_image, True, False, 0)
