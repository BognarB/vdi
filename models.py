from django.db import models

class ImageLibrary(models.Model):
    username = models.CharField(max_length=32)
    imageId = models.CharField(max_length=32)

class Instance(models.Model):
    username = models.CharField(max_length=32)
    instanceId = models.CharField(max_length=32)
