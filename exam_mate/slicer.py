from flask import Flask
from cubes.server import slicer

app = Flask(__name__)
app.register_blueprint(slicer, config="slicer.ini") 
# Optionally, add a URL prefix:
# app.register_blueprint(slicer, url_prefix="/slicer", config="slicer.ini")