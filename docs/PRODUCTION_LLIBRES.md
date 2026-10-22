# API del Banc: integración de Llibres en producción

Estado 22-10-2026: API actualizada y conectada con Commons. Llibres sigue siendo
un candidato interno; la publicación desde su interfaz permanece desactivada.

## Servicio y datos

- Imagen fijada: `sha256:4cb010c29a46b4e4dc8f4649d2c04eba813267c2d635806b1252fccb57d83854`.
- Compose: `/opt/recursos-api/docker/compose.yml`; entorno privado `service.env`,
  accesibles solo por root. Perfil reproducible en `compose.production.yml`.
- Docker host network con uvicorn exclusivamente en `127.0.0.1:8004`.
- Misma base `/var/lib/recursos-api/recursos.db`, montada en `/data`; se conservan
  favoritos, valoraciones, propuestas, catálogo y sesiones firmadas anteriores.
  Directorio de datos 0700 y base 0600, propiedad del usuario de servicio 1000;
  la sincronización existente utiliza una cuenta que conserva acceso.
- Usuario 1000, raíz de solo lectura, tmpfs de 64 MiB, 512 MiB de memoria, una CPU,
  sin capacidades ni socket Docker. El perfil debe tratar el tmpfs como una sola
  cadena: las comas dentro de listas YAML requieren comillas.
- La unidad antigua `recursos-api.service` queda desactivada, con código y venv
  conservados para reversión. El temporizador de sincronización sigue activo.

`RECURSOS_SEED=0` evita insertar datos de demostración al actualizar una instalación
existente. El comportamiento predeterminado de las instalaciones nuevas se conserva.

## Preparación y cambio

Se ensayó sobre copia consistente de la base, comparando cada fila y columna
preexistentes de sus nueve tablas. Se comprobó el contrato de las rutas públicas y
el frontend público anterior con favoritos persistentes contra la API candidata.
Publicación, aislamiento de autoría, envío privado del alumnado, aprobación de su
clase y retirada se ensayaron exclusivamente en copias con actores sintéticos.

La pausa de escritura afecta únicamente al prefijo `/api/` del Banc mediante
`/run/recursos-api-maintenance`. Se toma otra copia consistente tras detener el
escritor y se comprueban de nuevo todas las filas antes de retirar la pausa.
El primer intento detectó un tmpfs mal expresado; la reversión automática restauró
la API y su copia coherente antes de reabrir las escrituras. El perfil corregido
se arrancó y probó completo sobre la copia antes del cambio definitivo.

Preparación, copias, comparación y resultados privados:
`/opt/recursos-api/production-llibres/`. La copia cifrada anterior también se
recuperó desde el NAS y se verificaron sus archivos e integridad SQLite.

## Conexión con Commons y copias

El adaptador Llibres de la ID API utiliza `EDUTICTAC_RESOURCES_API_URL` y la
credencial interna `EDUTICTAC_ID_APP_TOKEN`, compartida solo entre ambos servidores.
No se cambia la clave de sesiones, credenciales OIDC ni códigos/PIN del alumnado.
Las rutas `/api/internal/` devuelven 404 en el nginx público del Banc.

La ID API pasa de la red bridge aislada a host network con escucha explícita en
`127.0.0.1:8005`, conservando imagen, volumen y restricciones. De ese modo alcanza
el Banc por loopback sin publicar un puerto adicional ni añadir otro proxy.
No basta con configurar `127.0.0.1:8004` dentro de su antigua red bridge.

Las copias automáticas cifradas de Commons incluyen ahora `resources/recursos.db`
y la configuración protegida del Banc. Se mantiene el temporizador existente,
retención y réplica al NAS; la recuperación comprueba integridad de ambas bases.
La base contiene los archivos de los libros publicados. El caché de miniaturas
es regenerable; los documentos docentes del sistema general requieren su propia
política de copia al habilitar esa función en la interfaz.

## Comprobaciones y reversión

123 pruebas API/configuración, 16 de copias y 6 de permisos de Commons correctas.
Sobre el candidato del VPS: publicación y revisión por clase con datos sintéticos.
Sobre servicios activos: salud 200, catálogo Llibres accesible desde Commons,
credencial interna coincidente y permisos anónimos denegados. Acceso docente
redirige al proveedor y callback anteriores; no se usa una cuenta real.

Después de reabrir las escrituras **no restaurar sin más una copia anterior**:
preservar primero la base actual, sus archivos WAL y la configuración. Una vuelta
atrás desactiva la publicación y cambia servicio/código conservando los datos
posteriores; el esquema añadido no reemplaza las tablas anteriores. No arrancar
simultáneamente la unidad antigua y el contenedor en el puerto 8004.

Pendiente antes de abrir Llibres: actualizar el frontend público del Banc (todavía
anterior a Vue y sin consulta de libros), preparar HTTPS del editor, habilitar su
interfaz de publicación y comprobar el circuito con cuentas reales del aula.
