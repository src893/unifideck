"""launcher.proton.language_setup — Pre-launch UI-language wiring.

Two layers, and keeping them apart is the whole design.

**The prefix's Windows locale** — ``Control Panel\\International`` in
``user.reg`` — is store-agnostic and applied once, from ``proton.dispatch``,
for every Windows launch. See :func:`~.prefix.apply_prefix_language`. This
used to be three identical per-store wrappers (Amazon, Ubisoft, and briefly
Battle.net), which meant Epic and GOG got no locale at all while the other
three each carried a copy of the same six lines.

**A store's own language setting** stays with that store, because it
configures the *game* rather than the prefix:

* Ubisoft's UPC ``Language`` key — :func:`~.ubisoft.apply_ubisoft_language`,
  still called from the Ubisoft handler;
* Battle.net's ``Client.Language`` — ``launcher/wrapper_locale``, which also
  drives the Agent's locale hint and therefore what an install downloads;
* Epic's ``-epiclocale`` and legendary's per-game ``config.ini``;
* GOG's ``--lang``, which gogdl bakes into ``goggame-*.info`` at install
  time. Rewriting that file at launch only corrupts GOG's own value, which
  is why nothing here touches it — a carve-out about ``goggame-*.info``
  specifically, not about the Windows locale, which GOG prefixes now get
  like every other store.
"""

from __future__ import annotations

from .prefix import apply_prefix_language
from .resolver import get_unifideck_language
from .ubisoft import apply_ubisoft_language

__all__ = [
    "apply_prefix_language",
    "apply_ubisoft_language",
    "get_unifideck_language",
]
