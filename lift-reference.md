# Lift reference: where to read an endpoint as it was before http4s

OBP-API's endpoints were written for Lift and migrated to http4s in May 2026. To tell whether a problem
predates the migration, read the endpoint's Lift code at the **last commit at which it was still live**
for its API version or standard (below), in the same `OBP_API_SOURCE` repository:

    git -C <source> show <last live>:obp-api/src/main/scala/code/api/v5_1_0/APIMethods510.scala
    git -C <source> grep -n <endpointName> <last live> -- obp-api/src/main/scala/code/api/v5_1_0/

Then compare it with the http4s version (`Http4sNNN.scala` in the same package now).

Lift also ran checks of its own before an endpoint's code, set by the endpoint's ResourceDoc
(`ResourceDoc.wrappedWithAuthCheck` in `obp-api/src/main/scala/code/api/util/APIUtil.scala`, at the same commit):

- the ResourceDoc's Roles (its last argument), unless `disableAutoValidateRoles()`;
- the bank, only if its error list has `$BankNotFound` and its URL `BANK_ID`;
- the account, only if `$BankAccountNotFound` and `BANK_ID` and `ACCOUNT_ID`;
- the view, only if `$UserNoPermissionAccessView` and `BANK_ID`, `ACCOUNT_ID` and `VIEW_ID`.

So a check that is missing from the endpoint's code may still have been done by Lift: read the ResourceDoc
and its error list too. A finding that started before the migration has its own history: trace it in the
Lift file (git log, blame) at the last live commit.

| API version or standard | Lift file(s) | Last live | Retired in |
|---|---|---|---|
| OBP v1.2.1 | `v1_2_1/APIMethods121.scala` | `cb6ba6213` | `4f5c5fc2f` 2026-05-21 |
| OBP v1.3.0 | `v1_3_0/APIMethods130.scala` | `08dddb7d6` | `c4683ea9f` 2026-05-21 |
| OBP v1.4.0 | `v1_4_0/APIMethods140.scala` | `7e7fabb09` | `b6d77655e` 2026-05-21 |
| OBP v2.0.0 | `v2_0_0/APIMethods200.scala` | `0b87ee0d8` | `40730353d` 2026-05-21 |
| OBP v2.1.0 | `v2_1_0/APIMethods210.scala` | `d9a7cfb3f` | `fae766c45` 2026-05-21 |
| OBP v2.2.0 | `v2_2_0/APIMethods220.scala` | `9fd53bb4a` | `71892f5cb` 2026-05-21 |
| OBP v3.0.0 | `v3_0_0/APIMethods300.scala` | `6efb28865` | `b78afa193` 2026-05-20 |
| OBP v3.1.0 | `v3_1_0/APIMethods310.scala` | `6446144e1` | `2a2051c14` 2026-05-20 |
| OBP v4.0.0 | `v4_0_0/APIMethods400.scala` | `64731e75f` | `0826ea5d4` 2026-05-20 |
| OBP v5.0.0 | `v5_0_0/APIMethods500.scala` | `b7788e83f` | `8858fd091` 2026-05-19 |
| OBP v5.1.0 | `v5_1_0/APIMethods510.scala` | `80b737c8d` | `88f46f854` 2026-05-19 |
| OBP v6.0.0 | `v6_0_0/APIMethods600.scala` | `669171dde` | `810589330` 2026-05-18 |
| AU Open Banking, Bahrain OBF, MxOF, Polish, STET | their `*Api.scala` files | `6795c3f53` | `d19af2b92` 2026-05-22 |
| Berlin Group v1.3 | `berlin/group/v1_3/*Api.scala` | `0b7c1ae49` | `8601d68cb` 2026-05-28 |
| UK Open Banking v2.0.0 | `UKOpenBanking/v2_0_0/APIMethods_UKOpenBanking_200.scala` | `30835d11d` | `8e774876a` 2026-05-28 |
| UK Open Banking v3.1.0: Accounts, AccountAccess, Balances, Transactions | those `*Api.scala` | `8e774876a` | `79c380f80` 2026-05-28 |
| UK Open Banking v3.1.0: the others | their `*Api.scala` | `30835d11d` | `8e774876a` 2026-05-28 |
| Resource docs (`ResourceDocs1_4_0`) | `ResourceDocsAPIMethods.scala` | `35988b04c` | `aabebc055` 2026-05-26 |
| Dynamic entities and endpoints | `dynamic/entity`, `dynamic/endpoint` | `82694c4f5` | `df5b1e684` 2026-05-29 |

Paths are under `obp-api/src/main/scala/code/api/`. A Lift file kept compiling after an endpoint was served by
http4s in some cases, so "last live" is the Lift code as last written, not proof that it served requests then.

The commented-out Lift files stayed in the tree until `206db4a68` (2026-09-01) deleted them; a checkout of the
commit before, `b4eda1bb8`, is kept at `OBP-API-before-lift-deletion` next to the OBP-API checkout, for people.
Its comments match the last live code, but git at the commits above is what the agents use.
