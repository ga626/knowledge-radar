# Privacy and Local State

KnowledgeRadar is designed so that a public checkout contains product code and configuration templates, not another user's configuration or activity.

Keep API keys in the repository-root `.env`; copy `.env.example` to create it. Browser logins, cookies, profiles, caches, task databases, reports, and runtime logs remain local. Do not copy any of them into a clone, issue, archive, or release upload.

The service runs on loopback by default. Its diagnostics expose a sanitized profile readiness summary; they do not return raw account records, profile directories, registry paths, or runtime note text.

Before publishing, build the two generated review artifacts and check them without printing matched values:

```bat
python scripts\build_product_lite_package.py
python scripts\verify_package_integrity.py --path dist\product-lite\KnowledgeRadar
python scripts\build_public_source_projection.py
python scripts\verify_package_integrity.py --path dist\public-source\KnowledgeRadar
```

The public-source projection contains only tracked files selected by `config\public-source-manifest.json`. The product package has a smaller runtime allowlist in `config\package-manifest.product-lite.json`. Both checks reject private file paths and common credential content patterns; an allowed template is not a substitute for a real secret scan during release review.
