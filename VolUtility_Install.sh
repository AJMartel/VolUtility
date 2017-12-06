cd ~
#Ubuntu 16.04 LTS
sudo apt-get update && sudo apt-get upgrade -y && sudo apt-get dist-upgrade -y
sudo apt-get -y install autoconf automake autopoint bison build-essential curl docker flex gcc gettext git libimage-exiftool-perl libjpeg8 libjpeg8-dev libmagic-dev libpcre++-dev libssl-dev libtool make nano pcregrep pkg-config python-dev python-lxml python-pip python-socks python-urllib3 python-yara unzip    
sudo -H pip install --upgrade pip

#MongoDB Install
sudo apt-key adv --keyserver hkp://keyserver.ubuntu.com:80 --recv EA312927
echo "deb http://repo.mongodb.org/apt/ubuntu xenial/mongodb-org/3.2 multiverse" | sudo tee /etc/apt/sources.list.d/mongodb-org-3.2.list
sudo apt-get update
sudo apt-get install -y mongodb-org
sudo service mongod start

mkdir git
cd git

#Yara Install
sudo -H pip install distorm3 pycrypto pillow ujson pyinstrument haystack pycoin simplejson pefile dpapick pysocks requests construct==2.5.5-reupload
git clone https://github.com/VirusTotal/yara
cd yara
sudo ./bootstrap.sh
./configure --with-crypto
make
sudo make install
sudo make check
cd ..

# Install SSDEEP
 curl -sSL http://sourceforge.net/projects/ssdeep/files/ssdeep-2.13/ssdeep-2.13.tar.gz/download | tar -xzC . 
 cd ssdeep-2.13 
 ./configure
 make 
 sudo make install 
 sudo -H pip install pydeep 
 cd .. 
 
 # Get the maxmind database for ip lookup
 curl -sSL http://geolite.maxmind.com/download/geoip/database/GeoLite2-City.mmdb.gz > GeoLite2-City.mmdb.gz 
 gzip -d GeoLite2-City.mmdb.gz 
 mkdir /usr/share/GeoIP 
 sudo mv GeoLite2-City.mmdb /usr/share/GeoIP/
 sudo -H pip install win-inet-pton
 
 # Install and Build libpff
 git clone https://github.com/libyal/libpff.git 
 cd libpff 
 ./synclibs.sh 
 ./autogen.sh 
 ./configure --enable-python 
 make 
 sudo make install 
 sudo ldconfig
 
#VirusTotal-api
sudo -H pip install virustotal-api

#Volatility Install
git clone https://github.com/volatilityfoundation/volatility.git
git clone https://github.com/AJMartel/profiles.git
rsync -a -r -v profiles/ volatility/
cd volatility
sudo python setup.py install
cd ..

#VolUtility Install
git clone https://github.com/AJMartel/VolUtility
cd VolUtility
sudo -H pip install -r requirements.txt
sudo nohup python manage.py runserver 0.0.0.0:8000 &

# Clean Up
sudo rm -rf ~/git
