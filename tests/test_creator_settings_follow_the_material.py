import json
import zipfile

from aethelark3d.slicers.elegoo import ElegooSlicerBackend as Backend

PROJECT = {
    "filament_settings_id": ["Bambu PLA Basic @BBL X1C"],
    "filament_type": ["PLA"],
    "different_settings_to_system": ["wall_loops;top_surface_speed;support_type;enable_support", "", ""],
    "wall_loops": "3",
    "top_surface_speed": ["50"],
    "enable_support": "1",
    "support_type": "tree(manual)",
    "support_style": "tree_hybrid",
    "support_filament": "0",
    "print_compatible_printers": ["Bambu Lab X1 Carbon 0.4 nozzle"],
    "printer_settings_id": "Bambu Lab X1 Carbon 0.4 nozzle",
}


SLIM_TREE = {"enable_support": "1", "support_type": "tree(auto)", "support_style": "tree_slim"}


def test_another_material_takes_nothing_from_the_creator():
    assert Backend._creator_settings(PROJECT, "eSUN ASA+") == SLIM_TREE


def test_the_same_material_follows_what_the_creator_changed():
    got = Backend._creator_settings(PROJECT, "Elegoo PLA")
    assert got["wall_loops"] == "3" and got["top_surface_speed"] == ["50"]
    assert {k: got[k] for k in SLIM_TREE} == SLIM_TREE


def test_silk_is_not_plain_pla():
    silk = dict(PROJECT, filament_settings_id=["Bambu PLA Silk @BBL X1C"])
    assert "wall_loops" not in Backend._creator_settings(silk, "Elegoo PLA")
    assert Backend._creator_settings(silk, "eSUN PLA Silk")["wall_loops"] == "3"


def test_supports_are_always_slim_tree_whatever_the_creator_chose():
    assert Backend._creator_settings(dict(PROJECT, enable_support="0"), "Elegoo PLA")["support_type"] == "tree(auto)"
    assert Backend._creator_settings(dict(PROJECT, enable_support="0"), "PLA")["enable_support"] == "1"


def test_the_project_is_rewritten_for_this_printer(tmp_path):
    src = tmp_path / "creator.3mf"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("3D/3dmodel.model", b'<metadata name="Application">BambuStudio-02.06.00.51</metadata>')
        z.writestr("Metadata/project_settings.config", json.dumps(PROJECT))
        z.writestr("Metadata/slice_info.config", b"x")
        z.writestr("Auxiliaries/Model Pictures/a.png", b"x")
    machine = tmp_path / "m.json"
    machine.write_text(json.dumps({"name": "Elegoo Centauri Carbon 0.4 nozzle", "printer_model": "Elegoo Centauri Carbon"}))
    process = tmp_path / "p.json"
    process.write_text(json.dumps({"name": "0.20mm Standard @Elegoo CC 0.4 nozzle", "wall_loops": "2"}))
    fil = tmp_path / "f.json"
    fil.write_text(json.dumps({"name": "Elegoo ASA", "nozzle_temperature": ["270"]}))

    out = Backend.__new__(Backend)._project_3mf(src, tmp_path, PROJECT, [machine, process], fil, "Textured PEI Plate")
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        body = json.loads(z.read("Metadata/project_settings.config"))
        model = z.read("3D/3dmodel.model")
    assert "Metadata/slice_info.config" not in names
    assert not any(n.startswith("Auxiliaries/") for n in names)
    assert body["printer_settings_id"] == "Elegoo Centauri Carbon 0.4 nozzle"
    assert body["print_settings_id"] == "0.20mm Standard @Elegoo CC 0.4 nozzle"
    assert body["filament_settings_id"] == ["Elegoo ASA"]
    assert body["printer_model"] == "Elegoo Centauri Carbon"
    assert body["print_compatible_printers"] == []
    assert body["nozzle_temperature"] == ["270"]
    assert b"BambuStudio-02.06" not in model


def test_a_setting_our_profiles_leave_out_takes_the_slicers_value_not_the_creators(tmp_path):
    """Elegoo's ASA profile never names the aux fan, so the creator's PLA 70%
    survived into an ASA print (2026-09-29, M106 P2 S178)."""
    project = dict(PROJECT, additional_cooling_fan_speed=["70"], brim_type="no_brim")
    src = tmp_path / "creator.3mf"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("3D/3dmodel.model", b"")
        z.writestr("Metadata/project_settings.config", json.dumps(project))
    machine = tmp_path / "m.json"
    machine.write_text(json.dumps({"name": "Elegoo Centauri Carbon 0.4 nozzle"}))
    baseline = {"additional_cooling_fan_speed": "0", "brim_type": "auto_brim"}
    out = Backend.__new__(Backend)._project_3mf(src, tmp_path, project, [machine], None,
                                                "Textured PEI Plate", baseline)
    with zipfile.ZipFile(out) as z:
        body = json.loads(z.read("Metadata/project_settings.config"))
    assert body["additional_cooling_fan_speed"] == ["0"]
    assert body["brim_type"] == "auto_brim"
