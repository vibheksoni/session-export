// Regenerates session_sdk/data/t3_state_v1.sql from a T3 Code checkout.
// Copy into apps/server/scripts/ of that checkout, then run:
//   node scripts/schema_dump.ts <empty-db-path> <output.sql>
// Runs T3 migrations 1..54 (the V1 schema) on the empty database, then dumps the schema and ledger.
import * as NodeFS from "node:fs";
import * as Effect from "effect/Effect";
import * as SqlClient from "effect/sql/SqlClient";
import * as NodeSqliteClient from "@t3tools/shared/nodeSqliteClient";

import { runMigrations } from "../src/persistence/Migrations.ts";

const [, , databasePath, outputPath] = process.argv;
if (!databasePath || !outputPath) {
  throw new Error("usage: unisessions-schema-dump.ts <empty-db-path> <output.sql>");
}

const program = Effect.gen(function* () {
  const sql = yield* SqlClient.SqlClient;
  yield* sql.unsafe("PRAGMA foreign_keys = ON").unprepared;
  const executed = yield* runMigrations({ toMigrationInclusive: 54 });
  const objects = yield* sql<{ readonly type: string; readonly name: string; readonly sql: string }>`
    SELECT type, name, sql FROM sqlite_master
    WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
    ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name`;
  const ledger = yield* sql<{ readonly migration_id: number; readonly name: string; readonly created_at: string }>`
    SELECT migration_id, name, created_at FROM effect_sql_migrations ORDER BY migration_id`;
  return { executed, objects, ledger };
}).pipe(Effect.provide(NodeSqliteClient.layer({ filename: databasePath })), Effect.scoped);

const { executed, objects, ledger } = await Effect.runPromise(program);

const lines: string[] = [];
for (const object of objects) {
  lines.push(`${object.sql};`);
}
for (const row of ledger) {
  const escapedName = row.name.replaceAll("'", "''");
  const escapedCreated = row.created_at.replaceAll("'", "''");
  lines.push(
    `INSERT INTO effect_sql_migrations (migration_id, name, created_at) VALUES (${row.migration_id}, '${escapedName}', '${escapedCreated}');`,
  );
}
NodeFS.writeFileSync(outputPath, lines.join("\n") + "\n");
console.log(`executed ${executed.length} migrations, wrote ${objects.length} objects and ${ledger.length} ledger rows to ${outputPath}`);
