from PIL import Image


def thumbnail(path):
    image = Image.open(path)
    image.thumbnail((128, 128))
    return image
