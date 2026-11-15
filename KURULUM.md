# Kurulum (Türkçe)

CloudTransfer, Google Drive'daki dosyaları **diske hiç yazmadan** OneDrive'a
aktarır. Dosya, Drive'dan parça parça okunur, bellekte tek bir parça tutulur ve
doğrudan OneDrive'a yazılır. Geçici klasör yoktur, yarım dosya kalmaz.

> **Önemli:** İki bulutu birbiriyle doğrudan konuşturmak mümkün değil. Google
> Drive'ın "şu adrese gönder" diye bir API'si yok; Microsoft'un "URL'den yükle"
> özelliği ise geri çekildi. Bu yüzden baytların bir aracıdan geçmesi
> gerekiyor. Bu yazılım o aracı. **Bilgisayarınıza hiç bayt gelmemesi için
> uygulamayı bir sunucuda veya konteynerde çalıştırın** — o zaman bilgisayarınız
> yalnızca arayüzü açar.

---

## 1. Google tarafı

1. [Google Cloud Console](https://console.cloud.google.com/) → proje oluşturun.
2. **APIs & Services → Library** → **Google Drive API**'yi etkinleştirin.
3. **OAuth consent screen** → External → kendinizi test kullanıcısı ekleyin.
4. **Credentials → Create credentials → OAuth client ID → Web application.**
5. Yetkili yönlendirme adresi:
   `http://localhost:8080/auth/google/callback`
6. Client ID ve Client Secret değerlerini kopyalayın.

Kullanılan izin `drive.readonly`'dir — uygulama Drive'ınızda hiçbir değişiklik
yapmaz.

## 2. Microsoft tarafı

1. [Azure Portal](https://portal.azure.com/) → **Microsoft Entra ID →
   App registrations → New registration**.
2. Hesap türü: kişisel **ve** iş/okul hesapları.
3. Redirect URI (tür: **Web**):
   `http://localhost:8080/auth/microsoft/callback`
4. **API permissions → Microsoft Graph → Delegated**: `Files.ReadWrite`,
   `offline_access`, `User.Read`.
5. **Certificates & secrets → New client secret** → **Value** sütunundaki
   değeri kopyalayın (Secret ID'yi değil).

## 3. Ayarlar

```bash
cp .env.example .env
```

`.env` içinde şu dört değeri doldurun:

```
CT_GOOGLE_CLIENT_ID=
CT_GOOGLE_CLIENT_SECRET=
CT_MS_CLIENT_ID=
CT_MS_CLIENT_SECRET=
```

Geri kalanların varsayılanı çalışır durumdadır.

## 4. Çalıştırma

### Kendi bilgisayarınızda

```bash
python -m venv .venv && .venv/bin/pip install -e .
```

```bash
.venv/bin/cloudtransfer serve
```

Ardından <http://localhost:8080> adresini açın.

### Konteynerde (bilgisayarınıza hiç trafik gelmez)

```bash
docker compose up -d
```

Uzak bir sunucuya kurarsanız `CT_BASE_URL` değerini `https://` adresiniz yapın
ve aynı adresi iki sağlayıcıda da yönlendirme adresi olarak kaydedin.

## 5. Kullanım

1. İki hesabı da bağlayın.
2. Soldan Drive klasörünü, sağdan OneDrive klasörünü seçin.
3. Aynı isim varsa ne olacağını seçin, "Start transfer" deyin.

Aktarım sırasında duraklatabilirsiniz; kaldığı **baytdan** devam eder. Bittiğinde
"Download report" ile her dosyanın durumunu, yeniden adlandırmaları ve atlanan
dosyaların gerekçelerini CSV olarak indirebilirsiniz.

## Bilmeniz gerekenler

- **Google Docs/Sheets/Slides** dosyaları `.docx`/`.xlsx`/`.pptx` olarak
  dönüştürülür. Google'ın dışa aktarma sınırı 10 MB'dır; bunu aşanlar gerekçesi
  yazılarak atlanır.
- **Google Forms, Sites ve My Maps** dışa aktarılamaz; bunlar atlanır.
- **OneDrive'ın kabul etmediği karakterler** (`" * : < > ? / \ |`) değiştirilir,
  aynı klasördeki aynı isimli dosyalar numaralandırılır. Her değişiklik rapora
  yazılır.
- **Drive'dan hiçbir şey silinmez.** Erişim salt okunurdur; bu bir kopyalamadır.
- Her dosya aktarımdan sonra karma (hash) ile doğrulanır.
