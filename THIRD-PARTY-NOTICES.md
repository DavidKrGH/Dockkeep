# Third-Party Notices

Dockkeep itself is licensed under the MIT License (see [LICENSE](LICENSE)).

This file lists third-party components that are redistributed as part of this
repository or of the published Docker image, together with their licenses.
It does not cover software that users install themselves.

## Bundled in this repository

These files are checked into the repository under `src/gui/static/js/`.

### htmx 1.9.12

- Project: https://htmx.org — https://github.com/bigskysoftware/htmx
- File: `src/gui/static/js/htmx.min.js`
- License: Zero-Clause BSD (0BSD)

0BSD imposes no attribution requirement; this entry and the accompanying
`src/gui/static/js/htmx.min.js.LICENSE.txt` are provided for transparency.

```
Zero-Clause BSD
=============

Permission to use, copy, modify, and/or distribute this software for
any purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
```

### Chart.js 4.4.4

- Project: https://www.chartjs.org — https://github.com/chartjs/Chart.js
- File: `src/gui/static/js/charts/chart.min.js`
- License: MIT

```
Copyright (c) 2014-2024 Chart.js Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Bundled in the Docker image

The published container image copies the following binaries from their
official upstream images (see `Dockerfile`).

### restic

- Project: https://restic.net — https://github.com/restic/restic
- License: BSD 2-Clause

```
Copyright (c) 2014, Alexander Neumann <alexander@bumpern.de>
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### rclone

- Project: https://rclone.org — https://github.com/rclone/rclone
- License: MIT

```
Copyright (C) 2012 by Nick Craig-Wood http://www.craig-wood.com/nick/

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

Dockkeep invokes `restic` and `rclone` as separate processes. It is not a
derivative work of either project.

Both are statically linked Go binaries that embed further open-source modules
under their own licenses. Those components and their license texts are listed
in the respective upstream repositories linked above.

## Python dependencies

Installed into the Docker image via `pip` (direct dependencies from
`pyproject.toml` plus their transitive dependencies). When working from a
source checkout these are installed by the user and are not redistributed by
this project. Versions are not pinned, so exact versions depend on the build;
the full license text of each package ships with it in its `*.dist-info`
directory inside the image.

| Package | License |
|---|---|
| annotated-doc | MIT |
| annotated-types | MIT |
| anyio | MIT |
| certifi | MPL-2.0 |
| click | BSD-3-Clause |
| croniter | MIT |
| fastapi | MIT |
| filelock | MIT |
| h11 | MIT |
| httpcore | BSD-3-Clause |
| httptools | MIT |
| httpx | BSD-3-Clause |
| idna | BSD-3-Clause |
| Jinja2 | BSD-3-Clause |
| MarkupSafe | BSD-3-Clause |
| pydantic | MIT |
| pydantic-core | MIT |
| python-dateutil | Apache-2.0 OR BSD-3-Clause |
| python-dotenv | BSD-3-Clause |
| python-multipart | Apache-2.0 |
| PyYAML | MIT |
| six | MIT |
| sse-starlette | BSD-3-Clause |
| starlette | BSD-3-Clause |
| tomlkit | MIT |
| typing-extensions | PSF-2.0 |
| typing-inspection | MIT |
| uvicorn | BSD-3-Clause |
| uvloop | MIT OR Apache-2.0 |
| watchfiles | MIT |
| websockets | BSD-3-Clause |

## Base image and system packages

The Docker image builds on `python:3.12-bookworm` (Debian) and installs
`gosu`, `bash-completion`, `openssh-client`, `postgresql-client`,
`default-mysql-client`, `redis-tools`, `sqlite3`, `curl`, `jq`,
`ca-certificates`, `docker-ce-cli` and `docker-compose-plugin` via `apt`.

These packages carry their own licenses, including copyleft licenses such as
the GPL. Each package's license text remains available inside the image under
`/usr/share/doc/<package>/copyright`, and the corresponding sources are
available from the Debian and Docker package repositories. All of these
programs are invoked as separate processes; they do not affect the licensing
of Dockkeep's own code.
