# 双班更正合流交接

## 签发

新增 `publication.commit.publish_merge(db, base_package_id, base_revision, left, right)`。前两个业务参数指向来源库中已经签发的 V1 件及其一个声明修订。按固定包的证据恢复该版完整行账。基版可以通过差额推出，例如 `B` 有可能没有自己的盘点。分区沿用基件。两个记录可以为空，按输入顺序解释。

`left` 和 `right` 各为列表或元组，每项为 `(operation_id, rows)`。操作号为非空 UTF-8 文本，在单个合流请求内识别操作。`rows` 沿用 README.md 中 V1 有符号差额的行格式。每笔差额内，同站点、槽、质控码的计数相加并去掉零项。每条记录内操作号唯一。两侧同号须有相同的规范化差额。差额为空的操作仍保留其身份与顺序。

合法执行恰好使用两条记录并集中每个操作号一次。把执行顺序分别投影到左、右记录的操作号，须分别得到两条记录的原序。每笔差额整笔应用，完成该笔后所有完整行重数须非负。两侧记录在共同执行中可以依赖另一侧产生的行。能找到任一合法执行即可签发。无解、无效输入、未知基件、未声明基版或使用 V2 作基件均报 `storage.EvidenceError`。

成功返回现有 `publication.receipts.Receipt`。`partition` 为基件分区，`revisions` 为 `('merged',)`，`inventory_ids` 和 `delta_ids` 为空元组，因为本次选件来自一个已签发基件。沿用现有签发目录和 `fetch`、`get_receipt`。包和回执同次提交。SQLite 签发异常报 `storage.StorageError`，包和回执一起回滚。

## V2 交换件

V2 是 UTF-8 JSON 字节。共有字段为 `version`、`partition`、`base_package_id`、`base_revision`、`base_rows`、`left`、`right`、`order`。`version` 为 JSON 整数 `2`。基件号为从 1 到 2⁶³−1 的整数，分区和基版为非空 UTF-8 文本。`base_rows` 是固定基版的完整行账，采用 V1 盘点行格式。`order` 是实际签发的操作号数组。全件采用 V1 的文本、精确整数和 JSON 对象键唯一规则。

记录有两种可选布局。每份实现选定一种，签发和接纳统一使用所选布局，并在交接说明中记录该选择。

- 内联布局只含共有字段。`left` 和 `right` 分别为 `[操作号, 差额行数组]` 的有序数组。两侧同号内容须在规范化后相同。
- 引用布局另含 `operations` 对象，键为操作号，值为该操作的差额行数组。`left` 和 `right` 是有序操作号数组。`operations` 的键集合必须恰等于两侧引用的并集。

两种布局都须保全两条记录的操作身份、原序和规范化差额，以及独立的实际执行顺序。基件号和基版记录白班所选来源，基账随件交付。夜班凭件内的基账验证合流，源库和单独的 V1 基件均可不在场。

## 接纳和核账

`offline.accept.import_package(db, blob)` 须在分配离线号前验证 V2 字段、基账、双侧操作身份、原序投影、执行覆盖及每笔后的重数。坏件报 `storage.PackageError` 且无新增条目。完整原字节重投返回原离线号。

`offline.recover.recover(db, package_id, 'merged')` 返回同 V1 排序和正重数规则的完整行列表。`merged` 是 V2 唯一可领取的结果修订。合法类型的未知修订报 `storage.NotFoundError`。原 `audit` 和 `fetch_audit` 入口须能核账、留存及重开取回 V2 结果。失败核账没有新增账号。保留原 V1 接口、包字节和历史结果。

## 样例与核验

`examples/merge-cases.json` 给出完整输入。`base_rows` 是手算基账，`left` 和 `right` 可直接传给签发入口。有 `order` 的样例列出该场景的唯一合法执行、恢复行和实际质控计数。其余样例应拒签。

- `net_is_not_enough` 的两条记录各自可执行，净终账为空，但开始执行任一侧后都不能完成整次合流。
- `backtrack` 从 V1 件中逆向取到 `B` 的一条 G。先执行 `a` 会堵住 `b`，合法顺序为 `b,c,a`，夜班应恢复一条 G。
- `shared` 的双侧 `s` 采用不同的写法，规范化后都减一。`s` 只执行一次，夜班应恢复两条 G。
- `opposite_shared_order` 的共有操作在两侧顺序相反。`different_shared_delta` 的同号操作规范化后仍有不同差额。两例均拒签。

保留 README.md 中的原回归和原演示。实现前可用 `python3 -B merge_examples.py fixtures` 核对样例基件。实现后沿 `.pointwise-runtime.json` 将 `python3 -B merge_examples.py verify` 接入核验，将 `python3 -B merge_examples.py demo` 接入演示。核验覆盖拒签、损坏凭据拒收、包及回执的共同提交和断源重开。核验失败须以非零状态退出。在本文记录所选交换布局、执行状态代表的截面及签发和接纳共同维护的正确性条件。
