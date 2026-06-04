import os
import sqlite3
import tempfile
import unittest

import app as nutrisnap_app


class MealSyncSchemaGuardTests(unittest.TestCase):
    def test_meal_sync_ensures_schema_before_upsert(self):
        fd, db_path = tempfile.mkstemp(prefix='nutrisnap-sync-', suffix='.db')
        os.close(fd)
        os.remove(db_path)

        old_get_sqlite_path = nutrisnap_app.get_sqlite_path
        old_core_schema_ready = nutrisnap_app.CORE_SCHEMA_READY
        nutrisnap_app.get_sqlite_path = lambda: db_path
        nutrisnap_app.CORE_SCHEMA_READY = False
        try:
            client = nutrisnap_app.app.test_client()
            token = nutrisnap_app.create_token('sync_schema_user')
            response = client.post(
                '/api/meals/sync',
                headers={'Authorization': f'Bearer {token}'},
                json={
                    'meals': [{
                        'client_id': 'meal_from_phone_1',
                        'session_id': 'voice_session_1',
                        'food_name': '西瓜',
                        'calories': 60,
                        'protein': 1,
                        'carbs': 15,
                        'fat': 0,
                        'weight': 150,
                        'portion': 1,
                        'created_at': '2026-06-04T03:56:00.000Z',
                        'updated_at': '2026-06-04T03:56:00.000Z',
                    }],
                    'deleted_client_ids': [],
                    'summaries': [{
                        'date': '2026-06-04',
                        'total_calories': 60,
                        'total_protein': 1,
                        'total_carbs': 15,
                        'total_fat': 0,
                    }],
                },
            )

            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            payload = response.get_json()
            self.assertTrue(payload['success'])
            self.assertEqual(payload['synced'], 1)
            self.assertEqual(payload['summaries'], 1)

            with sqlite3.connect(db_path) as conn:
                meal_count = conn.execute(
                    'SELECT COUNT(*) FROM meals WHERE username = ? AND client_id = ?',
                    ('sync_schema_user', 'meal_from_phone_1'),
                ).fetchone()[0]
                index_row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_meals_username_client_id'"
                ).fetchone()

            self.assertEqual(meal_count, 1)
            self.assertIsNotNone(index_row)
        finally:
            nutrisnap_app.get_sqlite_path = old_get_sqlite_path
            nutrisnap_app.CORE_SCHEMA_READY = old_core_schema_ready
            try:
                os.remove(db_path)
            except OSError:
                pass


if __name__ == '__main__':
    unittest.main()
