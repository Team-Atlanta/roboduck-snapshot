variable "REGISTRY" {
  default = "local"
}

variable "VERSION" {
  default = "latest"
}

function "tags" {
  params = [name]
  result = [
    "${name}:${VERSION}",
    "${name}:latest"
  ]
}

group "default" {
  targets = ["roboduck-base"]
}

target "roboduck-base" {
  context    = "."
  dockerfile = "oss-crs/dockerfiles/base.Dockerfile"
  tags       = tags("roboduck-base")
}
