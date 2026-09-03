import json
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.pipeline import (
    DmapRange,
    LUA_XOR_KEY,
    audio_sample_extension,
    asset_name_for_path,
    collect_web_audio_paths,
    decode_lua_asset,
    export_unity_objects,
    merge_localized_fields,
    is_current_client_metadata,
    parse_lua_literal,
    parse_lua_sheet,
    pack_excel_row,
    group_assets_by_bundle,
    group_selected_assets_by_bundle,
    output_asset_path,
    raw_client_asset_path,
    raw_lua_asset_path,
    safe_file_component,
    safe_output_path,
    text_asset_name_candidates,
    unity_asset_output_path,
)


def repeating_xor(data: bytes, key: bytes) -> bytes:
    return bytes(byte ^ key[index % len(key)] for index, byte in enumerate(data))


class MainTests(unittest.TestCase):
    def test_asset_name_for_lua_bytes_path(self):
        self.assertEqual(
            asset_name_for_path("LuaByte/Lua/Excels/item_definition.lua.bytes"),
            "item_definition.lua",
        )

    def test_text_asset_name_candidates_include_extensionless_names(self):
        self.assertEqual(
            text_asset_name_candidates("MyAssets/docs/config.json"),
            ("config.json", "config"),
        )
        self.assertEqual(
            text_asset_name_candidates("LuaByte/Lua/Excels/item_definition.lua.bytes"),
            ("item_definition.lua", "item_definition.lua.bytes", "item_definition"),
        )

    def test_output_asset_path_strips_bytes_suffix(self):
        self.assertEqual(
            output_asset_path("LuaByte/Lua/Excels/item_definition.lua.bytes"),
            "LuaByte/Lua/Excels/item_definition.lua",
        )

    def test_safe_output_path_preserves_logical_asset_path(self):
        root = Path("data").resolve()
        self.assertEqual(
            safe_output_path(root, "LuaByte/Lua/Excels/item_definition.lua.bytes"),
            root / "LuaByte" / "Lua" / "Excels" / "item_definition.lua.bytes",
        )

    def test_safe_output_path_rejects_traversal(self):
        root = Path("data").resolve()
        with self.assertRaises(ValueError):
            safe_output_path(root, "../outside.lua.bytes")

    def test_unity_asset_output_path_is_safe_and_unique(self):
        self.assertEqual(safe_file_component('icon:rank/"gold"'), "icon_rank__gold_")
        self.assertEqual(
            unity_asset_output_path(
                Path("assets"), "Sprite", 12, -34, 'icon:rank/"gold"', ".png"
            ),
            Path("assets").resolve() / "Sprite" / "0012" / "-34_icon_rank__gold_.png",
        )
        self.assertEqual(audio_sample_extension("voice.ogg"), ".ogg")
        self.assertEqual(audio_sample_extension("voice.fsb"), ".wav")

    def test_export_unity_objects_preserves_audio_format(self):
        clip = SimpleNamespace(m_Name="voice", samples={"voice.ogg": b"OggSdata"})
        obj = SimpleNamespace(
            type=SimpleNamespace(name="AudioClip"),
            path_id=7,
            read=lambda: clip,
        )
        with TemporaryDirectory() as directory, patch(
            "src.pipeline.UnityPy.load",
            return_value=SimpleNamespace(objects=[obj]),
        ):
            root = Path(directory)
            counts = export_unity_objects(b"bundle", root, 3)

            self.assertEqual(
                counts,
                {"Texture2D": 0, "Sprite": 0, "AudioClip": 1, "failed": 0},
            )
            self.assertEqual(
                (root / "AudioClip" / "0003" / "7_voice.ogg").read_bytes(),
                b"OggSdata",
            )

    def test_collect_web_audio_paths_uses_generated_tables(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)

            def write_table(relative_path, rows):
                path = root / "tables" / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(rows), encoding="utf-8")

            write_table("audio/audio.json", [{"path": "ui/click"}])
            write_table("audio/bgm.json", [{"path": "music/lobby.mp3"}])
            write_table(
                "item_definition/character.json",
                [{"sound": 1, "sound_folder": "yiji"}],
            )
            write_table("voice/sound.json", [{"id": 1, "path": "act_rich"}])
            write_table(
                "voice/event.json",
                [{"path": "audio/sound/event/greeting"}],
            )

            self.assertEqual(
                collect_web_audio_paths(root),
                [
                    "audio/music/lobby.mp3",
                    "audio/sound/event/greeting.mp3",
                    "audio/sound/yiji/act_rich.mp3",
                    "ui/click.mp3",
                ],
            )

    def test_group_assets_by_bundle_filters_prefixes(self):
        assets = [
            {"assetPath": "LuaByte/Lua/Excels/a.lua.bytes", "ownerBundleIndex": 10},
            {"assetPath": "LuaByte/Lua/Protol/b.lua.bytes", "ownerBundleIndex": 11},
            {"assetPath": "LuaByte/Lua/UI/c.lua.bytes", "ownerBundleIndex": 12},
            {"assetPath": "MyAssets/docs/config.json", "ownerBundleIndex": 13},
        ]

        grouped = group_assets_by_bundle(
            assets,
            prefixes=("LuaByte/Lua/Excels/", "LuaByte/Lua/Protol/"),
        )

        self.assertEqual(
            grouped,
            {
                10: ["LuaByte/Lua/Excels/a.lua.bytes"],
                11: ["LuaByte/Lua/Protol/b.lua.bytes"],
            },
        )

    def test_group_selected_assets_by_bundle_includes_client_data_and_support_lua(self):
        assets = [
            {"assetPath": "LuaByte/Lua/Excels/a.lua.bytes", "ownerBundleIndex": 10},
            {"assetPath": "LuaByte/Lua/Game/UIData/UI_Treasure_New_Data.lua.bytes", "ownerBundleIndex": 11},
            {"assetPath": "MyAssets/docs/proto_config.bytes", "ownerBundleIndex": 12},
            {"assetPath": "MyAssets/docs/spots/aiyin/aiyin04_kr.bytes", "ownerBundleIndex": 13},
            {"assetPath": "MyAssets/ui/common/main/pic/common/atlas_common_main_common_config.json", "ownerBundleIndex": 14},
            {"assetPath": "MyAssets/docs/contact_us_kr.txt", "ownerBundleIndex": 15},
        ]

        grouped = group_selected_assets_by_bundle(assets, ("LuaByte/Lua/Excels/",))

        self.assertEqual(
            grouped,
            {
                10: ["LuaByte/Lua/Excels/a.lua.bytes"],
                11: ["LuaByte/Lua/Game/UIData/UI_Treasure_New_Data.lua.bytes"],
                12: ["MyAssets/docs/proto_config.bytes"],
                13: ["MyAssets/docs/spots/aiyin/aiyin04_kr.bytes"],
                14: ["MyAssets/ui/common/main/pic/common/atlas_common_main_common_config.json"],
            },
        )

    def test_raw_client_asset_path_preserves_logical_asset_path(self):
        self.assertEqual(
            raw_client_asset_path("MyAssets/docs/proto_config.bytes"),
            "assets/MyAssets/docs/proto_config.bytes",
        )

    def test_raw_lua_asset_path_preserves_logical_asset_path_under_raw_root(self):
        self.assertEqual(
            raw_lua_asset_path("LuaByte/Lua/Net/ProtoDeclare.lua.bytes"),
            "lua/LuaByte/Lua/Net/ProtoDeclare.lua",
        )

    def test_decode_lua_asset_decrypts_repeating_xor_stream(self):
        plaintext = b'local a=require("ExcelTool")\nreturn a\n'
        encrypted = repeating_xor(plaintext, LUA_XOR_KEY)

        self.assertEqual(decode_lua_asset(encrypted), plaintext)

    def test_decode_lua_asset_keeps_plaintext_lua_source(self):
        plaintext = b'-- Generated By protoc-gen-lua Do not Edit\nlocal protobuf = require "protobuf.protobuf"\n'

        self.assertEqual(decode_lua_asset(plaintext), plaintext)

    def test_current_client_metadata_requires_same_profile_hash_and_version(self):
        metadata = {
            "bundle_profile": "ASTC",
            "bundle_hash": "hash-a",
            "product_version": "4.0.42",
        }
        client_info = {"product_version": "4.0.42"}

        self.assertTrue(
            is_current_client_metadata(metadata, client_info, "ASTC", "hash-a")
        )
        metadata["asset_mode"] = "all"
        self.assertTrue(
            is_current_client_metadata(
                metadata, client_info, "ASTC", "hash-a", asset_mode="all"
            )
        )
        self.assertFalse(
            is_current_client_metadata(
                metadata, client_info, "ASTC", "hash-a", asset_mode="text"
            )
        )
        self.assertFalse(
            is_current_client_metadata(metadata, client_info, "DXT", "hash-a")
        )
        self.assertFalse(
            is_current_client_metadata(metadata, client_info, "ASTC", "hash-b")
        )
        self.assertFalse(
            is_current_client_metadata(
                metadata,
                {"product_version": "4.0.43"},
                "ASTC",
                "hash-a",
            )
        )

    def test_parse_lua_literal_handles_generated_tables(self):
        self.assertEqual(
            parse_lua_literal('{["id"]=1,["name"]=1002,["enabled"]=false,[3]="x",nil}'),
            {"id": 1, "name": 1002, "enabled": False, 3: "x", 1: None},
        )

    def test_pack_excel_row_expands_dmap_and_defaults(self):
        fields = {
            "id": 1,
            "name": 1002,
            "type": 1003,
            "lifetime": 4,
            "speed": 5,
            "keypoint": 6,
        }
        defaults = [None, "Dapai", "hand_human", 900, 1, None]

        row = pack_excel_row(
            [DmapRange(2, 3), 1000101, 460, 0.94, [319, 319]],
            fields,
            defaults,
        )

        self.assertEqual(
            row,
            {
                "id": 1000101,
                "name": "Dapai",
                "type": "hand_human",
                "lifetime": 460,
                "speed": 0.94,
                "keypoint": [319, 319],
            },
        )

    def test_parse_lua_sheet_returns_readable_rows(self):
        source = (
            'local a=require("ExcelTool")'
            "local b=a.Pack;local c=a.Sub;local d=a.PackGet;local e=a.Dmap;"
            'local f={["id"]=1,["match_count"]=2,["item_id"]=3,["item_count"]=4}'
            "local g={9001,1,100002,20000}"
            "local h=function(i,j)return d(i,j,f,g)end;local k={}"
            "k[9001]={b({e[1002],0,0},h),b({false,2},h)}"
            "return{tb=k}"
        )

        sheet = parse_lua_sheet(source)

        self.assertEqual(
            sheet.rows,
            [
                {"id": 9001, "match_count": 1, "item_id": 0, "item_count": 0},
                {"id": 9001, "match_count": 2, "item_id": 100002, "item_count": 20000},
            ],
        )

    def test_merge_localized_fields_replaces_locale_indices_with_text(self):
        row = {
            "id": 200001,
            "sort": 1,
            "name_kr": 1,
            "name_en": 2,
            "desc_kr": None,
        }
        index_entry = {
            "TableName": "item_definition",
            "SheetName": "character",
            "kr": [
                "Excels.Langs.item_definition_character_name_kr",
                "Excels.Langs.item_definition_character_desc_kr",
            ],
            "en": [
                "Excels.Langs.item_definition_character_name_en",
            ],
        }
        locale_values = {
            "Excels.Langs.item_definition_character_name_kr": ["이치히메"],
            "Excels.Langs.item_definition_character_desc_kr": ["설명"],
            "Excels.Langs.item_definition_character_name_en": ["Ichihime", "Miki"],
        }

        self.assertEqual(
            merge_localized_fields(row, index_entry, locale_values),
            {
                "id": 200001,
                "sort": 1,
                "name": {
                    "kr": "이치히메",
                    "en": "Miki",
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
