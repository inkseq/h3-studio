# Credit admission: reader-first release N

This is the reader-only bridge selected in #409 review [6323385097](https://github.com/inkseq/h3-studio/issues/409#issuecomment-6323385097). It recognizes the preview `20261022_0007` marker and the exact additive `platform_budget_reservations.budget_role VARCHAR(20) NOT NULL DEFAULT 'enforced'` shape. The two permitted values are `accounting` and `enforced`. Quoted SQL literal contents remain exact when checking the finite SQLite/PostgreSQL grammars.

Frozen 005/006 definitions and runtime table metadata remain unchanged. Schema, payment and business-backup readers recognize 007. Both SQLite and native PostgreSQL business exports preserve the original role, IDs and amounts; malformed role shapes are refused. Actual PostgreSQL constraint reflection is covered by the existing explicit PostgreSQL CI manifest.

This release does not add credit-only admission, quote versions, role writes, admin-margin reporting or new public API fields. It preserves existing USD and version-1 credit behavior. There is no 007 adoption CLI, and its preview migration's `upgrade()` always refuses with `credit_admission_reader_only_release`. Importing or starting the application cannot adopt it.

Release N+1 will carry the separately reviewed admission/admin implementation and the guarded adoption path. Before any live DDL, verify the actual immutable current and retained platform artifacts both contain the bridge and pass the isolated native-006-copy reader proof. Unit tests of this source do not prove that artifact floor. A source merge, CI success, frontend-only release, empty commit or rebuilt old artifact cannot replace it.

No schema adoption, charging window, QA account/file change, grant, credential operation, generation, GPU allocation or production action is performed here. The adjusted staging fixture/window remains a later bounded scope under the same review; original QA identities and allowances stay unchanged. Combined PR #433 is preserved and unmerged until it is rebased onto the real reader release.
