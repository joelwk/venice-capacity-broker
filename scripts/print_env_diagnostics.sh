#!/usr/bin/env sh
#
# Print environment diagnostics with redacted sensitive values.
# Checks both environment variables (Secrets) and .env files.
#
# Usage: ./scripts/print_env_diagnostics.sh [context note]

detect_context() {
  # Prefer explicit Replit signals over generic Docker detection so deployments
  # running inside Replit containers are treated as 'replit' for diagnostics.
  if [ -n "${REPLIT_DEPLOYMENT:-}" ] || [ -n "${REPLIT_ENVIRONMENT:-}" ] || [ -n "${REPLIT_ENV:-}" ] \
     || [ -n "${REPLIT_DB_URL:-}" ] || [ -n "${REPL_ID:-}" ] || [ -n "${REPL_SLUG:-}" ]; then
    printf "replit"
  elif [ -f "/.dockerenv" ]; then
    printf "docker"
  else
    printf "local"
  fi
}

# Parse .env file and return value for a variable name
get_env_file_value() {
  var_name="$1"
  env_file="${2:-.env}"
  
  if [ ! -f "$env_file" ]; then
    return 1
  fi
  
  # Read .env file, skip comments and blank lines, handle quoted values
  while IFS= read -r line || [ -n "$line" ]; do
    # Remove leading/trailing whitespace
    line=$(printf '%s\n' "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    
    # Skip comments and empty lines
    [ -z "$line" ] && continue
    case "$line" in
      \#*) continue ;;
    esac
    
    # Handle export prefix
    case "$line" in
      export\ *) line="${line#export }" ;;
    esac
    
    # Check if this line defines our variable
    # Check if line starts with var_name=
    prefix="${var_name}="
    if [ "${line#${prefix}}" != "$line" ]; then
      # Extract value after =
      value="${line#${prefix}}"
      # Remove surrounding quotes if present
      case "$value" in
        \"*) value="${value#\"}"; value="${value%\"}" ;;
        \'*) value="${value#\'}"; value="${value%\'}" ;;
      esac
      printf '%s\n' "$value"
      return 0
    fi
  done < "$env_file"
  
  return 1
}

redact_value() {
  val="$1"
  printf '%s\n' "$val" | awk '
  {
    len = length($0)
    if (len <= 8) {
      printf "%s", $0
    } else {
      printf "%s****%s", substr($0, 1, 4), substr($0, len - 3, 4)
    }
  }
  '
}

# Enhanced print_field that checks both env and .env file
print_field() {
  name="$1"
  is_secret="$2"
  env_value=""
  env_file_value=""
  source=""
  is_optional=false
  
  # Check if this is a POSTGRES_* variable that can be derived from connection URL
  case "$name" in
    POSTGRES_HOST|POSTGRES_PORT|POSTGRES_DB|POSTGRES_USER|POSTGRES_PASSWORD)
      # Check if DATABASE_URL or SQL_DATABASE_URL is present
      if eval "[ \"\${DATABASE_URL+x}\" = x ]" || eval "[ \"\${SQL_DATABASE_URL+x}\" = x ]"; then
        db_url=""
        if eval "[ \"\${SQL_DATABASE_URL+x}\" = x ]"; then
          db_url=$(eval "printf '%s' \"\${SQL_DATABASE_URL}\"")
        elif eval "[ \"\${DATABASE_URL+x}\" = x ]"; then
          db_url=$(eval "printf '%s' \"\${DATABASE_URL}\"")
        fi
        # If connection URL is present and not empty/placeholder, POSTGRES_* are optional
        if [ -n "$db_url" ] && ! echo "$db_url" | grep -qE 'placeholder|example\.com|@host:|://host[:/]'; then
          is_optional=true
        fi
      fi
      ;;
  esac
  
  # Check environment variable (from Secrets)
  if eval "[ \"\${${name}+x}\" = x ]"; then
    env_value=$(eval "printf '%s' \"\${${name}-}\"")
  fi
  
    # Check .env file (only in replit/local context)
  context=$(detect_context)
  if [ "$context" = "replit" ] || [ "$context" = "local" ]; then
    if env_file_value=$(get_env_file_value "$name" ".env" 2>/dev/null); then    
      # Found in .env
      if [ -z "$env_value" ]; then
        # Only in .env, not in environment
        if [ "$is_optional" = true ]; then
          source=".env only (optional - derived from connection URL)"
        else
          source=".env only"
        fi
      else
        # In both
        if [ "$env_value" != "$env_file_value" ]; then
          source="env (differs from .env)"
        else
          source="env (.env matches)"
        fi
      fi
    else
      # Not in .env
      if [ -n "$env_value" ]; then
        source="Secrets only"
      elif [ "$is_optional" = true ]; then
        source="optional (derived from connection URL)"
      fi
    fi
  else
    # Docker context - only check environment
    if [ -n "$env_value" ]; then
      source="env"
    elif [ "$is_optional" = true ]; then
      source="optional (derived from connection URL)"
    fi
  fi

  # Special labelling for Replit SQL PG* helpers when running on Replit
  if [ "$context" = "replit" ] && [ -n "$env_value" ]; then
    case "$name" in
      PGHOST|PGPORT|PGDATABASE|PGUSER|PGPASSWORD)
        if [ -z "$source" ]; then
          source="env (Replit SQL)"
        fi
        ;;
    esac
  fi

  # Determine what to display
  if [ -n "$env_value" ]; then
    # Use environment value (Secrets take precedence)
    value="$env_value"
    display_value="$env_value"
  elif [ -n "$env_file_value" ]; then
    # Only in .env file
    value="$env_file_value"
    display_value="$env_file_value"
    if [ "$is_optional" != true ]; then
      source=".env only (NOT LOADED)"
    fi
  else
    # Not set anywhere
    if [ "$is_optional" = true ]; then
      # Optional variable - show as optional, not missing
      printf "○ %s=(not set) [optional - derived from connection URL]\n" "$name"
      return
    else
      printf "✗ %s=(not set)\n" "$name"
      if [ "$context" = "replit" ] && [ -f ".env" ]; then
        printf "  └─ Missing from both Secrets and .env\n"
      fi
      return
    fi
  fi
  
  # Handle empty values
  if [ -z "$value" ]; then
    printf "✗ %s=(empty)" "$name"
    if [ -n "$source" ]; then
      printf " [%s]" "$source"
    fi
    printf "\n"
    return
  fi
  
  # Redact if secret
  if [ "$is_secret" = "secret" ]; then
    display=$(redact_value "$display_value")
  else
    display="$display_value"
  fi
  
  len=${#value}
  printf "✓ %s=%s (%s chars)" "$name" "$display" "$len"
  if [ -n "$source" ]; then
    printf " [%s]" "$source"
  fi
  printf "\n"
  
  # Warn if only in .env (not loaded in Replit)
  if [ "$source" = ".env only (NOT LOADED)" ]; then
    printf "  └─ ⚠️  Variable is in .env but NOT loaded as environment variable\n"
    printf "  └─ Add to Replit Secrets or load .env file manually\n"
  elif [ "$source" = ".env only (optional - derived from connection URL)" ]; then
    printf "  └─ ℹ️  Optional: Variable is in .env but not needed (connection URL provides this)\n"
  fi
}

print_section() {
  section="$1"
  shift

  printf "\n[%s]\n" "$section"
  while [ $# -gt 0 ]; do
    name="$1"
    is_secret="$2"
    print_field "$name" "$is_secret"
    shift 2
  done
}

main() {
  note="$1"
  context=$(detect_context)
  timestamp=$(date +"%Y-%m-%d %H:%M:%S %Z")

  printf "\n=== ENV DIAGNOSTICS ===\n"
  printf "Context: %s\n" "$context"
  printf "Timestamp: %s\n" "$timestamp"
  if [ -n "$note" ]; then
    printf "Note: %s\n" "$note"
  fi
  
  # Show .env file status for replit/local contexts
  if [ "$context" = "replit" ] || [ "$context" = "local" ]; then
    if [ -f ".env" ]; then
      env_lines=$(grep -v '^#' .env | grep -v '^[[:space:]]*$' | grep -c '=' || echo "0")
      printf ".env file: present (%s variables)\n" "$env_lines"
    else
      printf ".env file: not found\n"
    fi
    printf "Replit Secrets: checked (environment variables)\n"
  fi

  print_section "Execution" "RUN_STACK_MODE" "" "APP_ENV" ""
  print_section "Database" \
    "SQL_DATABASE_URL" "secret" \
    "DATABASE_URL" "secret" \
    "POSTGRES_HOST" "" \
    "POSTGRES_PORT" "" \
    "POSTGRES_DB" "" \
    "POSTGRES_USER" ""

  print_section "Replit SQL (PG*)" \
    "PGHOST" "" \
    "PGPORT" "" \
    "PGDATABASE" "" \
    "PGUSER" "" \
    "PGPASSWORD" ""

  print_section "KV Store" \
    "REDIS_URL" "secret" \
    "KV_REDIS_URL" "secret" \
    "REPLIT_DB_URL" "secret" \
    "ALLOW_INMEMORY_KV_FALLBACK" ""

  print_section "Venice API" \
    "VENICE_API_BASE_URL" "" \
    "VENICE_API_KEY" "secret" \
    "VENICE_PARENT_KEY" "secret"

  print_section "Security" \
    "BROKER_ADMIN_TOKEN" "secret" \
    "SESSION_SECRET" "secret" \
    "ETH_PRIVATE_KEY" "secret" \
    "CDP_API_KEY_SECRET" "secret"

  print_section "On-chain" \
    "BASE_RPC_URL" "" \
    "BASE_CHAIN_ID" ""

  print_section "Risk" \
    "RISK_MAX_SLIPPAGE_BPS" "" \
    "RISK_MAX_POOL_TAKE_BPS" "" \
    "RISK_MAX_DIEM_TRADE_USD" "" \
    "RISK_MAX_DIEM_TRADE_UNITS" "" \
    "DIEM_PREMIUM_THRESHOLD" "" \
    "DIEM_DISCOUNT_THRESHOLD" "" \
    "DIEM_TARGET_SUPPLY" "" \
    "DIEM_LOCKED_SVVV_RATIO_CAP" "" \
    "DIEM_LOCKED_SVVV_RATIO_TARGET" ""

  print_section "Fair Value" \
    "DIEM_FAIR_VALUE_HORIZON_DAYS" "" \
    "DIEM_ADOPTION_BASE" "" \
    "DIEM_ILLIQUIDITY_DISCOUNT" "" \
    "VVV_FV_HORIZON_DAYS" "" \
    "VVV_FV_DISCOUNT_APY" "" \
    "VVV_FV_EMISSIONS_VVV_PER_DAY_PER_STAKED_VVV" "" \
    "VVV_FV_DIEM_PER_DAY_PER_STAKED_VVV" "" \
    "VVV_FV_DIEM_UTILITY_USD_PER_DIEM_DAY" "" \
    "VVV_FV_LOCKED_EMISSIONS_MULT" "" \
    "VVV_FV_PREFER_STAKE_DISCOUNT_MULT" ""

  print_section "Mode" \
    "AUTOSTART_ORCHESTRATOR_LIVE" "" \
    "AUTOSTART_STAKEMASTER_LIVE" ""

  # Summary for Replit context
  if [ "$context" = "replit" ]; then
    printf "\n[Summary]\n"
    printf "⚠️  Variables marked '[.env only (NOT LOADED)]' are in .env but not in Replit Secrets.\n"
    printf "   Replit does NOT automatically load .env files - use Secrets instead.\n"
    printf "   To fix: Add these variables to Replit Secrets (Tools → Secrets).\n"
    printf "\n   Replit SQL notes:\n"
    printf "   - Replit SQL creates DATABASE_URL and PG* (PGHOST, PGUSER, PGPASSWORD, PGDATABASE, PGPORT).\n"
    printf "   - This app prefers DATABASE_URL / SQL_DATABASE_URL as the source of truth; POSTGRES_* and PG* are helpers.\n"
    printf "   - POSTGRES_* vars are optional when a non-placeholder connection URL is present.\n"
  fi

  printf "========================\n\n"
}

main "$1"
