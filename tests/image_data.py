from io import BytesIO

from PIL import Image


def slika_bytes(format="PNG", size=(2, 2)):
    stream = BytesIO()
    Image.new("RGB", size, "white").save(stream, format=format)
    return stream.getvalue()
