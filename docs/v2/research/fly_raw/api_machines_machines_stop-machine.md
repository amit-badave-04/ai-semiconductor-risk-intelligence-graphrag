> ## Documentation Index
> Fetch the complete documentation index at: https://docs.fly.io/llms.txt
> Use this file to discover all available pages before exploring further.

# Stop Machine

> Stop a specific Machine within an app, with an optional request body to specify signal and timeout.




## OpenAPI

````yaml /api/machines/openapi.json post /v1/apps/{app_name}/machines/{machine_id}/stop
openapi: 3.1.0
info:
  title: Machines API
  description: >-
    This site hosts documentation generated from the Fly.io Machines API OpenAPI
    specification. Visit our complete [Machines API
    docs](https://fly.io/docs/machines/api/) for how to get started, more
    information about each endpoint, parameter descriptions, and examples.
  contact: {}
  license:
    name: Apache 2.0
    url: http://www.apache.org/licenses/LICENSE-2.0.html
  version: '1.0'
servers:
  - url: https://api.machines.dev
security:
  - FlyBearerAuth: []
tags:
  - name: Apps
    description: >-
      This site hosts documentation generated from the Fly.io Machines API
      OpenAPI specification. Visit our complete [Machines API
      docs](https://fly.io/docs/machines/api/apps-resource/) for details about
      using the Apps resource.
  - name: Machines
    description: >-
      This site hosts documentation generated from the Fly.io Machines API
      OpenAPI specification. Visit our complete [Machines API
      docs](https://fly.io/docs/machines/api/machines-resource/) for details
      about using the Machines resource.
  - name: TLS Certificates
    description: >
      This site hosts documentation generated from the Fly.io Machines API
      OpenAPI specification. Visit our complete [Machines API
      docs](https://fly.io/docs/machines/api/certificates-resource/) for details
      about using the TLS Certificates resource.
  - name: Volumes
    description: >-
      This site hosts documentation generated from the Fly.io Machines API
      OpenAPI specification. Visit our complete [Machines API
      docs](https://fly.io/docs/machines/api/volumes-resource/) for details
      about using the Volumes resource.
  - name: Postgres Clusters
    description: >-
      Create and manage Postgres clusters, including databases, users,
      extensions, backups, and app attachments.
externalDocs:
  url: https://fly.io/docs/machines/working-with-machines/
paths:
  /v1/apps/{app_name}/machines/{machine_id}/stop:
    post:
      tags:
        - Machines
      summary: Stop Machine
      description: >
        Stop a specific Machine within an app, with an optional request body to
        specify signal and timeout.
      operationId: Machines_stop
      parameters:
        - name: app_name
          in: path
          description: Fly App Name
          required: true
          schema:
            type: string
        - name: machine_id
          in: path
          description: Machine ID
          required: true
          schema:
            type: string
      requestBody:
        description: Optional request body
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/StopRequest'
        required: false
      responses:
        '200':
          description: OK
          content: {}
        '400':
          description: Bad Request
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
components:
  schemas:
    StopRequest:
      type: object
      properties:
        signal:
          type: string
          example: SIGTERM
          enum:
            - SIGHUP
            - SIGINT
            - SIGQUIT
            - SIGKILL
            - SIGUSR1
            - SIGUSR2
            - SIGTERM
        timeout:
          type: string
          example: 1s
    ErrorResponse:
      type: object
      properties:
        details:
          type: object
          description: Deprecated
        error:
          type: string
        status:
          $ref: '#/components/schemas/main.statusCode'
    main.statusCode:
      type: string
      enum:
        - unknown
        - insufficient_capacity
        - volume_placement_capacity
        - name_taken
      x-enum-varnames:
        - unknown
        - capacityErr
        - volumePlacementCapacityErr
        - nameTakenErr
  securitySchemes:
    FlyBearerAuth:
      type: http
      scheme: bearer
      description: >-
        A Fly API token with access to the requested organization or resource.
        Send it as `Authorization: Bearer <Fly API token>`.

````