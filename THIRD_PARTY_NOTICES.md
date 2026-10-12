# Third-Party Notices

The Architectural Lighting Control Interface ("Custom Web UI") is written by
Black Light Designs LLC (BLD) and licensed under the terms in [LICENSE](LICENSE).
It also depends on the third-party software below, each used under its own
license. Those licenses are separate from, and not limited by, BLD's license
or Terms of Service.

## Falcon Player (FPP)

The Software runs as a plugin on [Falcon Player](https://github.com/FalconChristmas/fpp),
which is developed independently of BLD and licensed under the GNU General
Public License, version 2 (GPLv2); individual FPP components may carry other
licenses. The Software talks to FPP through its network API and plugin
interface and reads and writes FPP configuration and media files. It does not
include, copy, or modify FPP's source code or binaries.

Animated effects are rendered by FPP's effects engine, including effects
derived from the [WLED](https://github.com/wled/WLED) project. They are
provided by FPP and are not part of BLD's code.

**Controllers delivered by BLD with FPP pre-installed:** FPP, the operating
system, and other software on the controller are distributed under their own
open source licenses. GPLv2 text: <https://www.gnu.org/licenses/old-licenses/gpl-2.0.html>.
For at least three years from delivery, BLD will provide the complete
corresponding source code for the FPP version delivered to anyone who asks, for
a charge no more than the cost of physically performing the distribution.
Request it at info@blacklightdesigns.com. The same source is published at
<https://github.com/FalconChristmas/fpp>.

## Python libraries

`fpp_install.sh` installs these into the plugin's private virtual environment
(`venv/`) from the Python Package Index. The full license text for each package
is kept in its `dist-info` folder inside that environment, for example
`venv/lib/python3.*/site-packages/<package>-<version>.dist-info/`.

Versions below are the ones resolved when this file was written; pip may
install newer compatible releases of the unpinned transitive packages.

### Direct dependencies (pinned in `requirements.txt`)

| Package | Version | License | Copyright / author |
|---|---|---|---|
| [Flask](https://github.com/pallets/flask) | 3.1.1 | BSD-3-Clause | Copyright 2010 Pallets |
| [Flask-SQLAlchemy](https://github.com/pallets-eco/flask-sqlalchemy) | 3.1.1 | BSD-3-Clause | Copyright 2010 Pallets |
| [Flask-Login](https://github.com/maxcountryman/flask-login) | 0.6.3 | MIT | Copyright (c) 2011 Matthew Frazier |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | 1.1.0 | BSD-3-Clause | Copyright (c) 2014 Saurabh Kumar and contributors |
| [bcrypt](https://github.com/pyca/bcrypt) | 4.2.1 | Apache-2.0 | The Python Cryptographic Authority developers |
| [requests](https://github.com/psf/requests) | 2.32.3 | Apache-2.0 | Kenneth Reitz and contributors |
| [waitress](https://github.com/Pylons/waitress) | 3.0.2 | ZPL-2.1 | Zope Foundation and contributors |

### Transitive dependencies

| Package | License | Copyright / author |
|---|---|---|
| Werkzeug | BSD-3-Clause | Copyright 2007 Pallets |
| Jinja2 | BSD-3-Clause | Copyright 2007 Pallets |
| MarkupSafe | BSD-3-Clause | Copyright 2010 Pallets |
| itsdangerous | BSD-3-Clause | Copyright 2011 Pallets |
| click | BSD-3-Clause | Copyright 2014 Pallets |
| blinker | MIT | Copyright 2010 Jason Kirtland |
| SQLAlchemy | MIT | Copyright 2005-2026 SQLAlchemy authors and contributors |
| urllib3 | MIT | Copyright (c) 2008-2020 Andrey Petrov and contributors |
| charset-normalizer | MIT | Copyright (c) 2025 TAHRI Ahmed R. |
| idna | BSD-3-Clause | Copyright (c) 2013-2026 Kim Davies and contributors |
| certifi | MPL-2.0 | Kenneth Reitz and contributors |
| typing_extensions | PSF-2.0 | Python Software Foundation and contributors |

On some platforms pip also installs `greenlet` (MIT / PSF-2.0) as a SQLAlchemy
dependency. The installer also upgrades `pip` and the environment includes
`setuptools`, both MIT licensed.

### Notes

* **certifi** is licensed under the Mozilla Public License 2.0. It is used
  unmodified; its source is at <https://github.com/certifi/python-certifi>.
* **bcrypt** and **requests** are licensed under the Apache License 2.0
  (<https://www.apache.org/licenses/LICENSE-2.0>). They are used unmodified.
* **Werkzeug** includes a debugger icon from the Silk icon set by Mark James
  (<http://www.famfamfam.com/lab/icons/silk/>), licensed under CC-BY-2.5 or
  CC-BY-3.0. The Software does not enable Werkzeug's debugger in production.
* No third-party JavaScript, CSS, fonts, or icon sets are bundled in the
  Software's templates; pages load no resources from external hosts.

## Trademarks

Falcon Player, FPP, WLED, Raspberry Pi, and other product names are the property
of their respective owners. Their use here identifies compatibility only and
implies no affiliation or endorsement.

Questions about these notices: info@blacklightdesigns.com
