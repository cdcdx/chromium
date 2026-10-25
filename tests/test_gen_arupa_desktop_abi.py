"""托管门面 ABI 面必须由内核 C 头派生（生成 / 同源校验 / 出包同步）。

事故背景（2026-10-11 static-25）：内核第八批加了 ARUPA_INTERCEPT_BLOCK_REQUEST=2，
C 头里有了，手写的 ArupaBrowser.cs 没跟上，出包又是 cp + 只覆盖门面源码的侧车摘要 ——
交付带着不匹配的 DLL 出包，宿主编译才炸 CS0117。

2026-10-12 批 11 又暴露两件事，本模块按它们收敛了语义：
  · 托管 wrapper 的源头在内核侧，他们又把枚举内联回文件里 → 生成物必须**按 wrapper 布局自适应**
    （内联的不再重复生成，但逐值核对；核不过就失败）；
  · 手工同步 wrapper 留下过 "ArupaBrowser copy.cs" → C# glob 到它就 CS0101/CS0111，要提前拦。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import gen_arupa_desktop_abi as abi

HEADER = """\
#define ARUPA_KERNEL_ABI_MAJOR 1
#define ARUPA_KERNEL_ABI_MINOR 36
typedef enum ArupaInterceptAction {
  // 合成响应替换
  ARUPA_INTERCEPT_SERVE_RESPONSE = 0,
  // With request.network_headers.v1, ...
  ARUPA_INTERCEPT_MODIFY_HEADERS = 1,
  // Requires arupa_kernel_supports("request.block.v1").
  ARUPA_INTERCEPT_BLOCK_REQUEST = 2,
  ARUPA_INTERCEPT_REDIRECT = 3,  // V3 response only
} ArupaInterceptAction;
#define ARUPA_BROWSER_COMMAND_TOGGLE_FULLSCREEN 1
#define ARUPA_BROWSER_COMMAND_OPEN_DEVTOOLS 2
typedef enum {
  ARUPA_KERNEL_NOT_STARTED = 0,
  ARUPA_KERNEL_RUNNING = 2,
} ArupaKernelState;
"""

# 与 HEADER 逐值对齐的"内联布局"wrapper：批 11 交付里的实际形态。
INLINE_WRAPPER = """\
public enum InterceptAction {
    ServeResponse = 0,
    ContinueWithModifiedHeaders = 1,
    BlockRequest = 2,
    Redirect = 3,   // ABI 1.36: real redirect
}
public enum BrowserCommand { ToggleFullscreen = 1, OpenDevTools = 2 }
public enum ArupaKernelState { NotStarted = 0, Running = 2 }
public static class ArupaAbi { }
public sealed partial class ArupaKernel { public static bool Supports(string feature) { return false; } }
"""

# "生成式"布局：wrapper 不管 ABI 面，全部交给 ArupaAbi.g.cs（所以它必须是 partial）。
GENERATED_LAYOUT_WRAPPER = ("// ABI 面（枚举/常量/探针）见 ArupaAbi.g.cs\n"
                            "public sealed partial class ArupaKernel { }\n")


class RenderTest(unittest.TestCase):
    def render(self, text=HEADER, wrapper=None):
        return abi.render(abi.parse_header(text, 'arupa_kernel_capi.h'), wrapper)

    def test_enum_values_are_derived_from_the_header(self):
        out = self.render()
        self.assertIn('BlockRequest = 2,', out)
        self.assertIn('Redirect = 3,', out)
        self.assertIn('public enum ArupaKernelState', out)
        self.assertIn('public static class ArupaAbi', out)
        self.assertIn('public const int Minor = 36;', out)

    def test_historical_public_names_survive_generation(self):
        """托管名与 C 名不一致的公开 API 不能在生成时被"顺手改名"。"""
        out = self.render()
        self.assertIn('ContinueWithModifiedHeaders = 1,', out)
        self.assertIn('OpenDevTools = 2,', out)
        self.assertNotIn('ModifyHeaders = 1,', out)

    def test_header_member_docs_carry_the_capability_requirement(self):
        self.assertIn('request.block.v1', self.render())

    def test_rendering_is_deterministic(self):
        self.assertEqual(self.render(), self.render())
        self.assertEqual(self.render(wrapper=INLINE_WRAPPER), self.render(wrapper=INLINE_WRAPPER))

    def test_inline_enums_are_verified_and_never_duplicated(self):
        """wrapper 已经内联的枚举不再生成（否则 CS0101），但要按 C 头核对通过。"""
        out = self.render(wrapper=INLINE_WRAPPER)
        self.assertNotIn('public enum InterceptAction', out)
        self.assertNotIn('public enum BrowserCommand', out)
        self.assertNotIn('public enum ArupaKernelState', out)
        self.assertIn('核对通过的内联枚举', out)
        self.assertIn('InterceptAction', out)

    def test_inline_enum_missing_a_value_is_a_hard_error(self):
        stale = 'public enum InterceptAction { ServeResponse = 0, BlockRequest = 2 }\n'
        with self.assertRaises(abi.GenError) as caught:
            self.render(wrapper=stale)
        self.assertIn('ContinueWithModifiedHeaders', str(caught.exception))
        self.assertIn('缺', str(caught.exception))

    def test_inline_enum_with_a_wrong_value_is_a_hard_error(self):
        wrong = 'public enum InterceptAction { ServeResponse = 0, ContinueWithModifiedHeaders = 1, BlockRequest = 9 }\n'
        with self.assertRaises(abi.GenError) as caught:
            self.render(wrapper=wrong)
        self.assertIn('值不符', str(caught.exception))

    def test_partial_kernel_is_required_for_the_generated_probe(self):
        """枚举都内联对了、但 ArupaKernel 不是 partial → 生成物里的探针补不进去，早报。"""
        not_partial = INLINE_WRAPPER.replace(
            'public sealed partial class ArupaKernel { public static bool Supports(string feature) { return false; } }',
            'public sealed class ArupaKernel { }')
        with self.assertRaises(abi.GenError) as caught:
            self.render(wrapper=not_partial)
        self.assertIn('partial', str(caught.exception))

    def test_missing_source_enum_is_a_hard_error(self):
        with self.assertRaises(abi.GenError):
            self.render('#define ARUPA_KERNEL_ABI_MAJOR 1\n#define ARUPA_KERNEL_ABI_MINOR 36\n')

    def test_parse_cs_enums_handles_single_line_and_implied_values(self):
        parsed = abi.parse_cs_enums('public enum E { A, B = 5, C }\npublic enum F { X = 1 }')
        self.assertEqual({'A': 0, 'B': 5, 'C': 6}, parsed['E'])
        self.assertEqual({'X': 1}, parsed['F'])


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='arupa abi test ')
        self.addCleanup(self.temp.cleanup)
        self.header = Path(self.temp.name) / 'arupa_kernel_capi.h'
        self.generated = Path(self.temp.name) / 'ArupaAbi.g.cs'
        self.header.write_text(HEADER)

    def test_generate_then_check_is_clean(self):
        self.assertTrue(abi.write_generated(self.header, self.generated))
        self.assertTrue(abi.write_generated(self.header, self.generated, check_only=True))

    def test_kernel_gains_an_action_and_the_generated_file_goes_stale(self):
        abi.write_generated(self.header, self.generated)
        self.header.write_text(HEADER.replace(
            '} ArupaInterceptAction;',
            '  ARUPA_INTERCEPT_HANDLE_AUTH = 4,\n} ArupaInterceptAction;'))
        self.assertFalse(abi.write_generated(self.header, self.generated, check_only=True))

    def test_generation_follows_the_sibling_wrapper_layout(self):
        """同目录 wrapper 里内联的枚举不进生成物 —— 布局变了，生成物就该跟着变。"""
        abi.write_generated(self.header, self.generated)
        first = self.generated.read_text(encoding='utf-8')
        (Path(self.temp.name) / abi.WRAPPER_NAME).write_text(INLINE_WRAPPER)
        abi.write_generated(self.header, self.generated)
        second = self.generated.read_text(encoding='utf-8')
        self.assertIn('public enum InterceptAction', first)
        self.assertNotIn('public enum InterceptAction', second)

    def test_explicit_dotnet_is_inherited_by_the_packaging_child(self):
        dotnet = Path(self.temp.name) / 'sdk' / 'dotnet'
        dotnet.parent.mkdir()
        dotnet.write_text('test SDK')
        with patch.dict(os.environ, {'ARUPA_DOTNET': str(dotnet)}):
            self.assertEqual(str(dotnet), abi.find_dotnet())


class DeliveryParityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='arupa abi delivery test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'include').mkdir()
        (self.root / 'include' / 'arupa_kernel_capi.h').write_text(HEADER)
        self.dotnet = self.root / 'dotnet'
        self.dotnet.mkdir()

    def write_wrapper(self, text):
        (self.dotnet / abi.WRAPPER_NAME).write_text(text)

    def test_fully_inline_wrapper_needs_no_generated_file(self):
        """内核侧把 wrapper 内联到底也能用：不重复生成，但只给个提示。"""
        self.write_wrapper(INLINE_WRAPPER)
        self.assertEqual([], abi.parity_refusals(self.root))
        self.assertEqual(1, len(abi.parity_warnings(self.root)))

    def test_stale_inline_enum_is_refused(self):
        self.write_wrapper('public enum InterceptAction { ServeResponse = 0, BlockRequest = 2 }\n')
        problems = abi.parity_refusals(self.root)
        self.assertEqual(1, len(problems))
        self.assertIn('不一致', problems[0])

    def test_new_layout_without_the_generated_file_is_refused(self):
        self.write_wrapper(GENERATED_LAYOUT_WRAPPER)
        problems = abi.parity_refusals(self.root)
        self.assertEqual(1, len(problems))
        self.assertIn(abi.GENERATED_NAME, problems[0])

    def test_generated_file_from_another_header_is_refused(self):
        self.write_wrapper(GENERATED_LAYOUT_WRAPPER)
        (self.dotnet / abi.GENERATED_NAME).write_text('// 另一份 C 头派生的\n')
        problems = abi.parity_refusals(self.root)
        self.assertEqual(1, len(problems))
        self.assertIn('不同源', problems[0])

    def test_duplicate_wrapper_copy_is_refused(self):
        """手工复制留下的副本会让 C# 工程重复定义门面类型（CS0101/CS0111）。"""
        self.write_wrapper(GENERATED_LAYOUT_WRAPPER)
        abi.write_generated(self.root / 'include' / 'arupa_kernel_capi.h',
                            self.dotnet / abi.GENERATED_NAME)
        self.assertEqual([], abi.parity_refusals(self.root))
        (self.dotnet / f'{abi.WRAPPER_NAME[:-3]} copy.cs').write_text(
            'public sealed class ArupaWebView { }\n')
        problems = abi.parity_refusals(self.root)
        self.assertEqual(1, len(problems))
        self.assertIn('copy.cs', problems[0])

    def test_matching_generated_file_passes(self):
        self.write_wrapper(GENERATED_LAYOUT_WRAPPER)
        abi.write_generated(self.root / 'include' / 'arupa_kernel_capi.h',
                            self.dotnet / abi.GENERATED_NAME)
        self.assertEqual([], abi.parity_refusals(self.root))
        self.assertEqual([], abi.parity_warnings(self.root))

    def test_stage_regenerates_the_delivery_copy(self):
        self.write_wrapper(GENERATED_LAYOUT_WRAPPER)
        self.assertEqual(0, abi.main(['--stage', str(self.root), '--no-build']))
        self.assertEqual([], abi.parity_refusals(self.root))


if __name__ == '__main__':
    unittest.main()
