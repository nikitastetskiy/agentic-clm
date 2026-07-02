# Product logos — sources & usage

HashiCorp product logos shown in the **top band of the featured / in-person event slides**. The
deterministic editor (`scripts/edit_event_slides.py` → `match_product_icons`) keeps only the logo of a
product the event actually covers (from `events.json` `products`) and drops the rest, so a Vault-only
session no longer shows the Terraform icon (and vice-versa). Partner logos (e.g. AWS) and the
HashiCorp/IBM brand lockup are never touched.

Each file is a **byte-identical copy of the source deck's own product-logo media**, so an exact
content hash unambiguously identifies which product a small top-band icon represents — with zero false
positives (a partner logo whose accent colour resembles a product's, e.g. AWS orange vs Vault gold,
never matches). The filename stem must equal the product key used in `events.json` `products`.

| File | Product | Extracted from |
|------|---------|----------------|
| `vault.png` | `vault` | source deck `ppt/media/image34.png` (gold Vault mark) |
| `terraform.svg` | `terraform` | source deck `ppt/media/image39.svg` (purple Terraform mark) |

**Updating:** if a future deck edition re-exports these icons with different bytes, re-extract them
here from that deck's media (classification degrades safely to a no-op until then — no icon is ever
wrongly removed). To manage another product, drop its deck-exported logo here named `<product>.<ext>`.
