import unittest

from origin_bridge.app import OriginBridgeApp


class DeferredRoot:
    def __init__(self) -> None:
        self.callbacks = []

    def after(self, _delay: int, callback) -> None:
        self.callbacks.append(callback)


class AppTests(unittest.TestCase):
    def test_export_failure_survives_deferred_callback(self) -> None:
        app = OriginBridgeApp.__new__(OriginBridgeApp)
        app.root = DeferredRoot()
        failures: list[str] = []
        app._export_failed = failures.append

        try:
            raise RuntimeError("测试导出失败")
        except Exception as exc:
            app._schedule_export_failure(exc)

        self.assertEqual([], failures)
        app.root.callbacks[0]()
        self.assertEqual(["测试导出失败"], failures)


if __name__ == "__main__":
    unittest.main()
