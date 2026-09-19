"""El rTSS de carrera lo calcula LaIA, porque Garmin no lo manda.

Comprobado contra la API real: Garmin emite trainingStressScore en el 100% de
las actividades de bici y en el 0% de las de carrera, aunque en carrera sí
mande potencia normalizada. El hueco es de Garmin, no del conector.
"""

import pytest

import training_data


def _carrera(np=297.0, duracion=3600.0):
    return {
        "activityType": {"typeKey": "running"},
        "normPower": np,
        "duration": duracion,
    }


def test_formula_conocida():
    """(NP/FTP)^2 * horas * 100 — con NP=FTP y una hora, exactamente 100."""
    r = training_data._rtss(_carrera(np=346.0, duracion=3600.0), ftp_carrera=346.0)
    assert r["rtss_estimado"] == pytest.approx(100.0)


def test_valores_reales_plausibles():
    """Los de la cuenta real: 60 min a 297 W con FTP 346 dan ~74."""
    r = training_data._rtss(_carrera(np=297.0, duracion=3600.0), ftp_carrera=346.0)
    assert r["rtss_estimado"] == pytest.approx(73.7, abs=0.5)


def test_se_puede_auditar_de_donde_sale():
    """Es un número nuestro, no de Garmin: hay que poder comprobarlo sin
    tener que creérselo."""
    r = training_data._rtss(_carrera(), ftp_carrera=346.0)
    calc = r["rtss_calculado_con"]
    assert calc["ftp_carrera_w"] == 346.0
    assert calc["potencia_normalizada_w"] == 297.0
    assert "formula" in calc
    assert "LaIA" in calc["nota"], "no queda claro que lo calculamos nosotros"


def test_en_bici_no_se_calcula():
    """En bici ya viene el de Garmin; mezclar los dos sería peor que nada."""
    bici = {"activityType": {"typeKey": "indoor_cycling"}, "normPower": 250.0, "duration": 3600.0}
    assert training_data._rtss(bici, ftp_carrera=346.0) == {}


@pytest.mark.parametrize(
    "actividad, ftp",
    [
        (_carrera(np=None), 346.0),        # sin potencia (cinta sin pod, por ejemplo)
        (_carrera(duracion=None), 346.0),  # sin duración
        (_carrera(), None),                # sin FTP configurado
    ],
)
def test_sin_datos_no_se_inventa(actividad, ftp):
    assert training_data._rtss(actividad, ftp_carrera=ftp) == {}


def test_los_codigos_sin_traducir_se_marcan():
    """Los códigos internos de Garmin se señalan para que nadie los
    interprete a ojo; los que no están en la respuesta no se listan."""
    respuesta = training_data._con_aviso_de_codigos(
        {"activities": [{"trainingEffectLabel": "AEROBIC_BASE", "duration": 60}]}
    )
    aviso = respuesta["codigos_sin_traducir"]
    assert aviso["campos"] == ["trainingEffectLabel"]
    assert "inventarlo" in aviso["nota"]


def test_sin_codigos_no_se_añade_ruido():
    respuesta = training_data._con_aviso_de_codigos({"activities": [{"duration": 60}]})
    assert "codigos_sin_traducir" not in respuesta
