import os

class DevelopmentConfiguration: 
    SECRET_KEY = os.environ.get('SECRET_KEY')
