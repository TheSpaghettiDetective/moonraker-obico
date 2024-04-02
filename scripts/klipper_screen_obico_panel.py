import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GdkPixbuf
from ks_includes.screen_panel import ScreenPanel
import qrcode
from io import BytesIO
import logging
import requests


class Panel(ScreenPanel):
    def __init__(self, screen, title):
        super().__init__(screen, title)

        # Create gtk items here

#        self.content.add(Gtk.Box())

        logging.info(self._printer.get_gcode_macros())

        url = 'http://localhost:7125/printer/objects/query?gcode_macro%20OBICO_LINK_STATUS'
        
        response = requests.get(url)
        response.raise_for_status()  # This will raise an exception for HTTP errors
        data = response.json()  # Assuming the response is JSON
        logging.info(data)
        
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.content.add(container)

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

        # Update the panel's display
        self.content.show_all()
