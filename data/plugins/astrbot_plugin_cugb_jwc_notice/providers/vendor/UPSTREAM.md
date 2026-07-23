# Vendored upstream

The `cugb_api` directory contains the list/detail clients, models, and HTML parsers selected from:

- Repository: `https://github.com/SuperL-2233/CUGBAPI`
- Commit: `6ae832cb6fb18f6ad85c2407043cad054cae2b6d`
- Upstream version: `2.1.0`
- License: MIT (see `LICENSE`)

Server, CLI, settings, API routing, and in-memory repository modules were not copied because the AstrBot plugin calls the public read-only sources directly and owns its own SQLite cache.

The JWC boards (student zone and teaching updates) use `NoticeClient`; the AI-college and student-affairs boards use `CmsNoticeClient` with the appropriate detail path prefix.

This package replaces the previously vendored `cugb_jwc_api` from `SuperL-2233/CUGBJwcAPI` (`d9e3696`, v1.1.0), which upstream renamed and extended in the 2.x line.
