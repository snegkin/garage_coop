"""
Фото гаража (GaragePhoto) на странице garages.detail: URL картинки несёт
?v=<имя файла>, чтобы браузер не показывал из кэша старое фото, когда
файл заменили («Сохранить» с новым файлом) или удалили фото и загрузили
другое (SQLite может выдать тот же id).
"""
from app.models import RoleEnum, GaragePhoto

from tests.conftest import make_garage, make_user, login


def test_photo_url_changes_when_file_replaced(db, client):
    garage = make_garage(db, number="990")
    photo = GaragePhoto(garage_id=garage.id, file_path="aaa111.jpg")
    db.add(photo)
    make_user(db, "board990", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board990", "pass12345")

    html = client.get(f"/garages/{garage.id}").get_data(as_text=True)
    assert f"/garages/photos/{photo.id}/file?v=aaa111.jpg" in html

    photo.file_path = "bbb222.jpg"
    db.commit()
    html = client.get(f"/garages/{garage.id}").get_data(as_text=True)
    assert f"/garages/photos/{photo.id}/file?v=bbb222.jpg" in html
    assert "v=aaa111.jpg" not in html
