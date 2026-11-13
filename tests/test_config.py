from sistema.config import Config


def test_por_defecto_apunta_a_la_red_de_planta():
    cfg = Config.desde_entorno({})
    assert (cfg.mqtt_host, cfg.mqtt_port, cfg.sim_url) == ("broker", 1883, "http://sim:8100")
    assert cfg.latido_s == 30.0


def test_las_variables_de_entorno_pisan_los_valores_con_su_tipo():
    cfg = Config.desde_entorno({"MQTT_HOST": "localhost", "MQTT_PORT": "11883", "LATIDO_S": "5"})
    assert cfg.mqtt_host == "localhost"
    assert cfg.mqtt_port == 11883
    assert cfg.latido_s == 5.0
