# Maintainer: Bradley <bradley@gennakersystems.com>

pkgname=voiceagent
pkgver=0.1.0
pkgrel=3
pkgdesc='Push-to-talk KDE-friendly desktop voice assistant'
arch=('any')
url='https://github.com/pizzimenti/voiceagent'
license=('MIT')
options=(!strip !debug)
depends=(
  'python'
  'pyside6'
  'aria2'
  'portaudio'
)
makedepends=(
  'python-build'
  'python-installer'
  'python-pip'
  'python-setuptools'
  'python-wheel'
)

build() {
  cd "${startdir}"
  rm -rf build dist src/*.egg-info "${srcdir}/vendor"
  python -m build --wheel --no-isolation

  python -m pip install \
    --upgrade \
    --target "${srcdir}/vendor" \
    --requirement packaging/vendor-requirements.txt \
    --only-binary=:all: \
    --disable-pip-version-check \
    --no-compile
}

package() {
  cd "${startdir}"
  python -m installer --destdir="${pkgdir}" dist/*.whl

  install -d "${pkgdir}/usr/lib/voiceagent"
  cp -a "${srcdir}/vendor" "${pkgdir}/usr/lib/voiceagent/vendor"
  find "${pkgdir}/usr/lib/voiceagent/vendor" -name '*.pyc' -delete

  install -Dm755 packaging/voiceagent-launcher "${pkgdir}/usr/bin/${pkgname}"
  install -Dm644 LICENSE "${pkgdir}/usr/share/licenses/${pkgname}/LICENSE"
  install -Dm644 packaging/voiceagent.desktop \
    "${pkgdir}/usr/share/applications/${pkgname}.desktop"
}
