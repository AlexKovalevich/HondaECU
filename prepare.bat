python.exe -m venv venv 
venv\Scripts\activate.bat 
pip install -r requirements.txt
python.exe --version
cd eculibDistrib
python.exe setup.py install
cd ..
copy drivers/ftdiForPythonFix/venv venv
#python.exe eculib/setup.py install
#python.exe src/__main__.py
venv\Scripts\python.exe src/__main__.py

