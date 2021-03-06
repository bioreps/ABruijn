ABruijn Installation
====================

Availability
------------

ABruijn is available for Linux and MacOS platforms. Windows support is not guaranteed.


Requirements
------------

* C++ compiler with C++11 support (GCC 4.8+ / Clang 3.3+ / Apple Clang 5.0+)
* GNU make
* Python 2.7
* Git
* Basic OS development headers (zlib, etc.)


ABruijn Installation
--------------------

ABruijn is written in both C++ and Python and requires a Python 2.7 environment.

First, to build ABruijn, run:

    python setup.py build

ABruijn could be invoked with the following command:

    bin/abruijn

Additonally, you may install the package for the better OS integration:

    python setup.py install

Alternatively, you can perform local user installation by adding '--user' or '--prefix'
options to the previous command.
