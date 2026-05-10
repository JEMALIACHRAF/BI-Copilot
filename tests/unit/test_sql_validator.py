"""Unit tests for the SQL validator — fast, no I/O."""

import pytest

from src.tools.sql_validator import extract_table_refs, validate


class TestValidateAcceptsValidQueries:
    def test_simple_select_passes(self):
        result = validate("SELECT name FROM `proj.ds.users` LIMIT 100")
        assert result.is_valid
        assert result.error is None
        assert "LIMIT" in result.normalized_sql

    def test_query_with_cte_passes(self):
        sql = """
        WITH active AS (SELECT id FROM `proj.ds.users` WHERE last_login > '2024-01-01')
        SELECT COUNT(*) AS n FROM active
        """
        result = validate(sql)
        assert result.is_valid

    def test_query_with_window_function_passes(self):
        sql = """
        SELECT user_id,
               ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) AS rn
        FROM `proj.ds.events`
        LIMIT 1000
        """
        result = validate(sql)
        assert result.is_valid

    def test_trailing_semicolon_is_stripped(self):
        result = validate("SELECT 1 AS x;")
        assert result.is_valid


class TestValidateInjectsLimit:
    def test_limit_is_injected_when_missing(self):
        result = validate("SELECT id FROM `proj.ds.users`", row_cap=500)
        assert result.is_valid
        assert "LIMIT 500" in result.normalized_sql.upper()

    def test_existing_limit_is_preserved(self):
        result = validate("SELECT id FROM `proj.ds.users` LIMIT 50")
        assert result.is_valid
        assert "LIMIT 50" in result.normalized_sql.upper()


class TestValidateRejectsBadSql:
    def test_empty_string_is_rejected(self):
        result = validate("")
        assert not result.is_valid
        assert result.error_kind == "syntax"

    def test_syntax_error_is_caught(self):
        result = validate("SELEKT * FROMM users")
        assert not result.is_valid
        assert result.error_kind == "syntax"

    def test_multiple_statements_are_rejected(self):
        result = validate("SELECT 1; SELECT 2")
        assert not result.is_valid

    @pytest.mark.parametrize(
        "sql",
        [
            "DELETE FROM `proj.ds.users`",
            "UPDATE `proj.ds.users` SET name = 'x'",
            "INSERT INTO `proj.ds.users` VALUES (1)",
            "DROP TABLE `proj.ds.users`",
            "CREATE TABLE `proj.ds.users` (id INT64)",
        ],
    )
    def test_mutating_statements_are_rejected(self, sql):
        result = validate(sql)
        assert not result.is_valid


class TestExtractTableRefs:
    def test_finds_qualified_table(self):
        refs = extract_table_refs("SELECT * FROM `proj.ds.users`")
        assert "proj.ds.users" in refs

    def test_finds_multiple_tables_in_join(self):
        sql = """
        SELECT u.id, o.amount
        FROM `proj.ds.users` u
        JOIN `proj.ds.orders` o ON u.id = o.user_id
        """
        refs = extract_table_refs(sql)
        assert "proj.ds.users" in refs
        assert "proj.ds.orders" in refs

    def test_invalid_sql_returns_empty(self):
        assert extract_table_refs("not even sql") == []
