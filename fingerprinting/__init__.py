from os.path import abspath, join, dirname

from flask import Flask
from flask_sqlalchemy import SQLAlchemy

from ngs_utils.utils import is_us


DATA_DIR = abspath(join(dirname(__file__), '..', 'data'))


app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////' + join(DATA_DIR, 'projects.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)


if is_us():
    HOST_IP = '172.18.72.171'
    PORT = 5001
else:
    HOST_IP = 'localhost'
    PORT = 5003


def get_version():
    try:
        from fingerprinting import version
    except ImportError:
        version = None
    else:
        version = version.__version__
    return version
