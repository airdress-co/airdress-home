# airdress

The Airdress Python packages. **You probably want
[`airdress-home`](https://pypi.org/project/airdress-home/).**

This project is a working meta-package, not an empty reservation. Installing it
installs `airdress-home`, and `import airdress.home` is that package:

```python
import airdress.home

key = airdress.home.MachineKey.generate()
```

| Package | Import | What it is |
| --- | --- | --- |
| [`airdress-home`](https://pypi.org/project/airdress-home/) | `airdress_home` | Link a home hub such as Home Assistant to an airdress |

It is released together with `airdress-home`, from
[airdress-co/airdress-home](https://github.com/airdress-co/airdress-home) by
trusted publishing, and at least once a year.

On PyPI's rules for project names (PEP 541): this project has a function (it
installs and re-exports the real package), and it is maintained. Whether that
is enough is for PyPI's administrators to judge; this README is the case we
make.

Licensed under the Apache License 2.0.
