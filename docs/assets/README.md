# README artwork

[Repository overview](../../README.md)

The README uses local SVGs so its banner, technology logos and status badges render without an external badge service, web fonts or image CDN. The artwork contains no installation names, public addresses or credentials.

## Status updates

The three status badges summarize documented readiness as of **28 October 2026**. Deployment and removal refer to the [readiness report](../deployment-readiness.md); **HA acceptance: Runbook provided** links to the [test runbook](../ha-acceptance.md) and does not claim completed HA testing. Update the report, README status table, badge labels and SVG descriptions together when new validation evidence is available. The Removal badge records the user-confirmed successful removal of one deployed installation. A removal Plan alone does not demonstrate successful deletion, and successful deployment does not demonstrate HA acceptance.

## Logo provenance

Brand paths in `readme-banner.svg` and `technology-stack.svg` are embedded from [Simple Icons](https://github.com/simple-icons/simple-icons/tree/90646e4bf7f3b89df6c37e65baf3e7bc759a517c) at the pinned revision `90646e4bf7f3b89df6c37e65baf3e7bc759a517c`. Paths retain their geometry and use contrasting fills. The banner, layout and status badges are repository artwork.

| Mark | Upstream source | Brand or license reference |
| --- | --- | --- |
| Ansible | [Source](https://www.ansible.com/logos) | See upstream source |
| Argo | [Source](https://github.com/cncf/artwork/blob/c2e619cdf85e8bac090ceca7c0834c5cfedf9426/projects/argo/icon/black/argo-icon-black.svg) | See upstream source |
| Cloudflare | [Source](https://www.cloudflare.com/logo/) | [Reference](https://www.cloudflare.com/trademark/) |
| Keycloak | [Source](https://github.com/keycloak/keycloak-misc/blob/dee033f2d6d6b5c3a6ce8eb84e285f7e5626dbf6/logo/icon-black.svg) | [Reference](https://www.linuxfoundation.org/legal/trademark-usage) |
| OPNsense | [Source](https://github.com/opnsense/core/blob/f4b69b9b565d2747eb40d7d23e060f4a3c81a071/src/opnsense/www/themes/opnsense/build/images/icon-logo.svg) | See upstream source |
| Red Hat OpenShift | [Source](https://www.openshift.com) | See upstream source |
| PostgreSQL | [Source](https://wiki.postgresql.org/wiki/Logo) | [Reference](https://www.postgresql.org/about/policies/trademarks/) |
| Vault | [Source](https://www.hashicorp.com/brand) | [Reference](https://www.hashicorp.com/brand) |

The Simple Icons package license is included as [CC0-1.0](SIMPLE-ICONS-LICENSE.txt). Individual brand artwork and trademarks may carry separate terms; see the linked references and [upstream disclaimer](https://github.com/simple-icons/simple-icons/blob/90646e4bf7f3b89df6c37e65baf3e7bc759a517c/DISCLAIMER.md). Logos identify the technologies used; they do not indicate sponsorship, endorsement or enterprise support for the community Keycloak operator.

SVGs have explicit backgrounds and accessible titles/descriptions. Keep paths local, avoid scripts or external SVG resources, and check legibility at README width after editing.
